#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ue5_bridge.py — UE5 自动化混合通道 · HTTP 写入口
==================================================
⚠️  角色变更（2026-07-23）：HTTP 层从「全功能入口」降为「写通道」。
    读写分离架构：
      - 读（health/read）→ ue5_runner.py → ue_send.py → UE Remote Execution
      - 写（build/connect）→ ue5_runner.py → HTTP POST → 本服务（:8889）
    本服务仍为写操作主力通道，持续维护。

FastAPI 服务，默认端口 8889（可通过环境变量 UE5_BRIDGE_PORT 覆盖）。
通过 GameThread 原子执行封装 unreal Python API + C++ Bridge DLL。

端点：
  GET  /health     → Bridge状态 + UE版本 + 项目路径
  POST /command    → 白名单命令模式（替代旧 /execute，杜绝任意代码执行）
  POST /read       → 读取蓝图类信息/变量/CDO (L1) + 节点/连线 (L2)
  POST /build      → 创建蓝图资产/添加变量/组件/编译
  POST /connect    → 连线（调用修复后的 Bridge DLL，三重验证）
  POST /disconnect → 断开引脚连线（v0.7 新增 · T-20260801-FIX-ENDPOINTS）
  POST /delete_node→ 删除节点（v0.7 新增 · T-20260801-FIX-ENDPOINTS）
  POST /build-batch → 批量创建蓝图（v0.6 P4：原子化，失败回滚）

依赖：
  - unreal Python 模块（UE5 编辑器内运行）
  - remote_execution（UE Python Script Plugin）
  - BlueprintPythonBridge C++ 插件（L2 节点读写）

启动方式：
  UE5 编辑器 → Window → Developer Tools → Python Console →
    import ue5_bridge; ue5_bridge.start()

作者：dev（代码开发 Agent）
日期：2026-07-22
"""

import json
import sys
import os
import time
import math  # v6.5：锁 TTL / 场景超时的 NaN/Inf 夹取
import copy  # v6.5：幂等结果缓存（返回副本，防调用方改坏缓存）
import shutil  # v5.0 资产快照/恢复（文件级备份）
import traceback
import hashlib
import threading
import atexit  # 🔧 T-20260805-CRASH-FIX: UE 关闭时自动 stop 服务
import tempfile
import uuid
import secrets  # T-20260908-CPPAPIS-AUDIT-FIX P2-2: compare_digest 恒定时间比较
import queue as _queue
import re  # T-20261024-UE5BRIDGE-COMPILE-DIAGNOSE：编译报错行结构化解析（原先在 _cmd_compile_blueprint 内局部导入）
import inspect  # T-20261028-TOOL-SEARCH：从 lambda/实现函数反射命令目录
import base64  # T-20261024-UE5BRIDGE-SCREENSHOT：ue5_screenshot 的 image content block 编码
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel

# ── FastAPI ──────────────────────────────────────────────
try:
    from fastapi import FastAPI, HTTPException, Request, Body
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, FileResponse, Response
    import uvicorn
except ImportError:
    print("[ue5_bridge] FastAPI/uvicorn 未安装。请在 UE Python 中执行：")
    print("  subprocess.call([sys.executable, '-m', 'pip', 'install', 'fastapi', 'uvicorn'])")
    raise

# ── Unreal ───────────────────────────────────────────────
_TEST_MODE = os.environ.get("UE5_BRIDGE_TEST_MODE") == "1"

try:
    import unreal
except ImportError:
    if _TEST_MODE:
        # 🧪 测试模式（UE5_BRIDGE_TEST_MODE=1）：注入 fake_unreal 替身
        import sys as _sys
        _scripts_dir = os.path.dirname(os.path.abspath(__file__))
        if _scripts_dir not in _sys.path:
            _sys.path.insert(0, _scripts_dir)
        import fake_unreal as unreal
        print("[ue5_bridge] TEST MODE: unreal uses fake_unreal (UE5_BRIDGE_TEST_MODE=1)")
    else:
        raise ImportError(
            "unreal 模块不可用。ue5_bridge 必须在 UE5 编辑器的 Python 环境中运行。"
        )

# ── Bridge DLL ───────────────────────────────────────────
try:
    bridge = unreal.BlueprintPythonBridge
    BRIDGE_AVAILABLE = True
except AttributeError:
    bridge = None
    BRIDGE_AVAILABLE = False
    print("[ue5_bridge] [WARN] BlueprintPythonBridge C++ 插件未加载，L2 节点操作不可用")

# ── App ──────────────────────────────────────────────────
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app: "FastAPI"):
    """FastAPI 生命周期（T-20260908-DEPRECATION-FIX）。

    等价替换原 @app.on_event("startup") / @app.on_event("shutdown")，
    消除 FastAPI on_event DeprecationWarning。行为不变：
    启动置 _server_running=True + 打印诊断；关闭置 False + 打印。
    """
    # ── startup（原 on_startup）──
    global _server_running
    _server_running = True
    ue_info = _get_ue_info()
    print("[ue5_bridge] service started v0.5.0")
    print(f"  UE version: {ue_info.get('engine_version', 'unknown')}")
    print(f"  Project:     {ue_info.get('project_name', 'unknown')}")
    print(f"  Bridge:   {'LOADED' if BRIDGE_AVAILABLE else 'NOT LOADED (L2 unavailable)'}")
    print(f"  Port:     8889")

    # ── API 可用性诊断 ──
    if BRIDGE_AVAILABLE:
        key_apis = {
            "add_event_node": hasattr(bridge, "add_event_node"),
            "add_function_call_node": hasattr(bridge, "add_function_call_node"),
            "add_function_node": hasattr(bridge, "add_function_node"),
            "find_node_by_title": hasattr(bridge, "find_node_by_title"),
            "connect_pins": hasattr(bridge, "connect_pins"),
            "list_pins": hasattr(bridge, "list_pins"),
            "get_node_connections": hasattr(bridge, "get_node_connections"),
            "set_pin_default_value": hasattr(bridge, "set_pin_default_value"),
            "get_event_graph": hasattr(bridge, "get_event_graph"),
        }
        available = [k for k, v in key_apis.items() if v]
        missing = [k for k, v in key_apis.items() if not v]
        print(f"  API diag: {len(available)}/{len(key_apis)} available")
        if available:
            print(f"    available: {', '.join(available)}")
        if missing:
            print(f"    missing: {', '.join(missing)}")

    try:
        yield
    finally:
        # ── shutdown（原 on_shutdown）──
        # global 已在函数开头声明（作用于整个函数），此处直接赋值
        _server_running = False
        print("[ue5_bridge] service stopped")


app = FastAPI(
    title="UE5 Automation Bridge",
    version="0.5.0",  # 🔧 T-20260801-FIX-ENDPOINTS: +/disconnect +/delete_node
    description="UE5 蓝图自动化 HTTP 入口 — 与maintainer Agent 系统对接",
    lifespan=lifespan,
)

# ── CORS 安全策略（v0.5 B5 修复：\d+$ 防 Userinfo 绕过 · T-20260727-C02：追加 IPv6 [::1]）────
# T-20260801-FIX-ENDPOINTS: allow_origin_regex 必须是单个正则 str（新 Starlette 不接受 list），
# 多正则用 | 连接（原逗号分隔语义保持等价）。
_ALLOWED_ORIGIN_REGEX = "|".join(
    p for p in os.environ.get(
        "UE5_BRIDGE_CORS_ORIGINS",
        r"http://localhost:\d+$,http://127\.0\.0\.1:\d+$,http://\[::1\]:\d+$"
    ).split(",")
    if p
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_ALLOWED_ORIGIN_REGEX,  # B5 修复
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 仪表盘数据管道（T-20261024-UE5BRIDGE-DASHBOARD · /ui 只读观测面）──
# 纯标准库 + fastapi，零 UE 依赖；单向依赖（bridge → ui_dashboard，反向会成环）。
# import 失败不许拖垮 bridge：观测面整组降级（/ui 返回 503），命令面照常。
try:
    import sys as _sys_dash
    _scripts_dir_dash = os.path.dirname(os.path.abspath(__file__))
    if _scripts_dir_dash not in _sys_dash.path:
        _sys_dash.path.insert(0, _scripts_dir_dash)
    import ui_dashboard as _ui_dash
    _UI_INDEX = os.path.join(os.path.dirname(_scripts_dir_dash), "ui", "index.html")
    _DASHBOARD_AVAILABLE = True
except Exception as _dash_err:  # pragma: no cover - 观测面整组降级路径
    _ui_dash = None
    _UI_INDEX = ""
    _DASHBOARD_AVAILABLE = False
    print(f"[ue5_bridge] [WARN] 仪表盘数据管道不可用，/ui 观测面降级: {_dash_err}")

# 任务日志（仪表盘 v2 数据脊柱）：agent 上报意图/计划/进度/复盘 → 面板渲染脉络。
# 同为主机侧纯 Python，零 UE 依赖；import 失败只降级任务命令面（schema fail-loud
# 在模块内部），命令面照常。
try:
    import task_journal as _task_journal
    _TASK_JOURNAL_AVAILABLE = True
except Exception as _tj_err:  # pragma: no cover
    _task_journal = None
    _TASK_JOURNAL_AVAILABLE = False
    print(f"[ue5_bridge] [WARN] 任务日志不可用，task_* 命令降级: {_tj_err}")

# ── trace_id 中间件（T-20260806-UE5BRIDGE-CRASH-FIX 方案E · 执剑者补充）──
# 目标：每个请求分配 trace_id（X-Trace-Id 头或随机 8 位 hex），贯穿日志/响应头，
#       崩溃复发时按 trace_id 区分触发路径（队列路径 vs execute_python_on_game_thread 路径②）。
# 注意：middleware 保持 async 是 uvicorn 推荐用法，不影响端点改为同步 def（线程池）。
#
# ── T-20261024-UE5BRIDGE-DASHBOARD：事件记录挂在本中间件（观测面）──
# 为什么挂这里：本中间件是所有请求（/mcp //command//read//build…）的唯一咽喉，
#   零侵入拿到 route/method/status/latency/trace_id —— stdio 代理
#   （ue5_mcp_stdio.py:40）固定转发到本服务，所以无论用户用哪个 MCP 客户端，
#   流量都过这里，这里记事件 = 全量观测。
# tool_name 不在这里取：中间件读请求体会消费掉流式 body（只能读一次，读了就毁，
#   MCP 客户端直接断连）——tool_name 由 /mcp 处理器解析 body 后写到 request.state，
#   本中间件收尾时读取（见 ui_dashboard.note_tool_name / tool_name_of）。
#
# ★★ 刻意的 fail-open（对 fail-loud 纪律的明确例外，全仓唯一）★★
#   事件记录的任何异常（磁盘满 / JSONL 不可写 / 缓冲异常 / request.state 不可读）
#   一律 try/except 吞掉，绝不阻塞命令主路径。理由：观测面失效不许影响业务 ——
#   agent 写操作的可信度由返回体 verified/warnings 承担，不依赖仪表盘；一个
#   "为了让人类看见"的旁路把命令弄挂是本末倒置。该例外只覆盖事件记录这一处，
#   /ui* 只读端点本身照常 fail-loud（查不到就 404/401，不假装成功）。
def _record_event_safe(request: Request, status: int, t0: float,
                       trace_id: str, error: bool = False) -> None:
    """事件记录的 fail-open 包装（观测面专用，见 add_trace_id 内例外声明）。

    注入测试的挂点：把 ui_dashboard.record_event 换成必然抛错的记录器后，
    本函数吞掉异常、命令照常成功（tests/test_dashboard.py 回归锁定）。
    """
    try:
        _latency = (time.perf_counter() - t0) * 1000.0
        _tool = _ui_dash.tool_name_of(request)
        _ui_dash.record_event(
            route=request.url.path, method=request.method, status=status,
            latency_ms=_latency, trace_id=trace_id,
            tool_name=_tool, error=error)
        # T-20261025-UX：任务自动进度 + 证据计数（agent 不必逐步 task_step；
        # success 声明需证据 —— 见 task_journal.note_tool_use）
        if _TASK_JOURNAL_AVAILABLE and _tool:
            _task_journal.note_tool_use(
                _tool, ok=(200 <= int(status or 0) < 300), latency_ms=_latency)
    except Exception:
        pass  # ★ fail-open：观测面永不阻塞命令主路径


# v6.5：HTTP 请求体上限（防超大 JSON 把编辑器内存打爆）。
# 只按 Content-Length 预检（不读 body，避免消费一次性流）；可用环境变量覆盖。
try:
    _MAX_REQUEST_BYTES = max(1024, int(os.environ.get("UE5_MAX_REQUEST_BYTES", "8388608")))
except (TypeError, ValueError):
    _MAX_REQUEST_BYTES = 8 * 1024 * 1024


@app.middleware("http")
async def add_trace_id(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id", uuid.uuid4().hex[:8])
    request.state.trace_id = trace_id
    if request.method in ("POST", "PUT", "PATCH"):
        cl = request.headers.get("content-length")
        if cl:
            try:
                if int(cl) > _MAX_REQUEST_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "请求体过大（上限 %d 字节，"
                                           "UE5_MAX_REQUEST_BYTES 可调）"
                                           % _MAX_REQUEST_BYTES})
            except (TypeError, ValueError):
                return JSONResponse(
                    status_code=400, content={"detail": "Content-Length 非法"})
    _t0 = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        # ── 事件记录（T-20261024-UE5BRIDGE-DASHBOARD，fail-open 见上）──
        _record_event_safe(request, response.status_code, _t0, trace_id)
        return response
    except Exception:
        # 异常路径也记事件（status=500 + error 标记），再原样上抛 ——
        # 记录本身吞异常，但业务异常不吞（fail-loud 原则在业务面不变）
        _record_event_safe(request, 500, _t0, trace_id, error=True)
        raise

# ── Token 认证（A-004 修复：原零认证 / v0.5 修复：消除硬编码 test123）──
# v2.20 修复（E-11 冷启动竞态）：旧实现在模块导入时**无条件**重新生成随机 token
#   并覆盖 token 文件 → 冷启动序列「UE 已开但 bridge 未启 → 宿主 runner 先读
#   token 文件（旧值）→ ensure_http_bridge 启动 bridge（生成新值覆盖文件）→
#   宿主用旧值轮询 /health → 恒 403 → 误报『bridge 启动后无响应』」。
#   现在：token 文件已存在（非空）→ **复用**；只有文件缺失/为空/不可读时才生成。
#   UE5_BRIDGE_TOKEN 环境变量仍然最高优先。
_BRIDGE_TOKEN = os.environ.get("UE5_BRIDGE_TOKEN")

if not _BRIDGE_TOKEN:
    # v6.5：显式 token 文件路径（测试/多实例隔离用；默认 %APPDATA%/ue5_bridge/token）
    _token_file = os.environ.get("UE5_BRIDGE_TOKEN_FILE", "").strip()
    if not _token_file:
        _token_dir = os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")), "ue5_bridge")
        _token_file = os.path.join(_token_dir, "token")
    else:
        _token_dir = os.path.dirname(os.path.abspath(_token_file))
    _existing_token = None
    try:
        with open(_token_file, "r", encoding="utf-8") as _tf:
            _existing_token = _tf.read().strip() or None
    except OSError:
        _existing_token = None
    if _existing_token:
        _BRIDGE_TOKEN = _existing_token
    else:
        _BRIDGE_TOKEN = secrets.token_hex(16)
        try:
            os.makedirs(_token_dir, exist_ok=True)
            with open(_token_file, "w") as f:
                f.write(_BRIDGE_TOKEN)
            # T-20260727-C01: Token 移至 %APPDATA%/ue5_bridge/token 私有目录
        except OSError:
            pass  # 写文件失败不影响 bridge 运行


def _verify_token(request) -> bool:
    """验证请求的 X-Skill-Token 头（v3.0 扩展两项兼容通道）：

    ① `Authorization: Bearer <token>` —— 标准 MCP 客户端 headers 配置；
    ② `?token=<token>` 查询参数 —— 不支持自定义 header 的 MCP 客户端的兜底
       （⚠️ token 会出现在 URL 中，仅限本机回环使用；能用 header 就别用这个）。
    """
    if not _BRIDGE_TOKEN:
        return False
    headers = getattr(request, "headers", {})
    token = headers.get("x-skill-token", "") or headers.get("X-Skill-Token", "")
    if not token:
        auth = headers.get("authorization", "") or headers.get("Authorization", "")
        if isinstance(auth, str) and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        try:
            qp = getattr(request, "query_params", None)
            if qp is not None:
                token = qp.get("token", "") or ""
        except Exception:
            token = ""
    if isinstance(token, list):
        token = token[0] if token else ""
    return secrets.compare_digest(str(token), _BRIDGE_TOKEN)


# ── 操作日志 ──────────────────────────────────────────
import logging as _logging
_OP_LOG = _logging.getLogger("ue5_bridge.operations")
_OP_LOG.setLevel(_logging.INFO)
if not _OP_LOG.handlers:
    _h = _logging.StreamHandler()
    _h.setFormatter(_logging.Formatter(
        "[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    _OP_LOG.addHandler(_h)

# ── 全局状态 ────────────────────────────────────────────
_start_time = time.time()
_request_count = 0
_lock = threading.Lock()


# ══════════════════════════════════════════════════════════
#  核心 Helper 函数
# ══════════════════════════════════════════════════════════

class BridgeError(Exception):
    """Bridge 自定义异常，携带分类标签用于降级决策。"""
    def __init__(self, message: str, category: str = "unknown",
                 pitfall_id: str = "", suggestion: str = ""):
        super().__init__(message)
        self.category = category        # node_failure / pin_failure / compile_error / ...
        self.pitfall_id = pitfall_id    # 对应 pitfalls.md 编号（P01-P08）
        self.suggestion = suggestion


# ── 中英文节点标题映射（解决中文 UE 标题匹配暗坑）────
# 当 UE 编辑器运行在中文环境时，蓝图节点标题会被本地化。
# 此映射表用于 find_node_by_title 失败时的回退查找。
_NODE_TITLE_I18N = {
    # Event nodes
    "BeginPlay":            ["事件开始运行", "开始运行", "BeginPlay"],
    "Tick":                 ["事件 Tick", "Tick"],
    "EndPlay":              ["事件结束运行", "结束运行", "EndPlay"],
    "ConstructionScript":   ["构造脚本", "Construction Script"],
    # Function nodes
    "PrintString":          ["打印字符串", "Print String"],
    "Delay":                ["延迟", "Delay"],
    "Branch":               ["分支", "Branch"],
    "Sequence":             ["序列", "Sequence"],
    "DoOnce":               ["只执行一次", "Do Once"],
    "SetTimer":             ["设置定时器", "Set Timer"],
    "SpawnActor":           ["生成 Actor", "Spawn Actor"],
    "DestroyActor":         ["销毁 Actor", "Destroy Actor"],
    "GetPlayerController":  ["获取玩家控制器", "Get Player Controller"],
    "GetActorLocation":     ["获取 Actor 位置", "Get Actor Location"],
}


def _find_node_by_title_i18n(graph, title: str):
    """多语言节点查找：先精确匹配 → 中文回退 → 全图遍历。

    UE 节点标题在中文环境下会被本地化（如 BeginPlay → 事件开始运行）。
    此函数依次尝试：
      1. 直接 find_node_by_title（英文精确匹配）
      2. 查 i18n 映射表，尝试中文变体
      3. 全图遍历，模糊匹配（最后手段）
    """
    # Step 1: 直接查找
    node = unreal.BlueprintPythonBridge.find_node_by_title(graph, title)
    if node:
        return node

    # Step 2: i18n 映射表回退（英文→中文变体）
    candidates = _NODE_TITLE_I18N.get(title, [title])
    for variant in candidates:
        if variant != title:
            node = unreal.BlueprintPythonBridge.find_node_by_title(graph, variant)
            if node:
                return node

    # Step 3: 全图遍历（最后手段——遍历所有节点逐一匹配）
    try:
        all_nodes = unreal.BlueprintPythonBridge.list_nodes(graph)
        for node_info in all_nodes:
            node_title = str(node_info.node_title)
            if node_title == title:
                node_obj = unreal.BlueprintPythonBridge.find_node_by_title(graph, node_title)
                if node_obj:
                    return node_obj
            # 模糊匹配：检查 i18n 候选
            for variant in candidates:
                if node_title == variant:
                    node_obj = unreal.BlueprintPythonBridge.find_node_by_title(graph, node_title)
                    if node_obj:
                        return node_obj
    except Exception:
        pass

    return None


def _find_bp_node_info(graph, node_id):
    """list_nodes 遍历查找 BPNodeInfo（与 get_pin_infos / set_pin_default_value 兼容）。

    T-20260805-UE-FULL-CLOSURE 修复：_find_node_by_title_i18n 返回 K2Node（C++ 原生节点
    对象），而 Bridge 的 GetPinInfos / SetPinDefaultValue 期望 BPNodeInfo（NodeInfo
    结构体）——必须用 list_nodes 返回的 BPNodeInfo 对象才能正确调用（已实测验证）。

    T-20260820-UE5BRIDGE-SET-FIX：增加 i18n 标题回退——UE 中文环境下节点标题可能被
    本地化（如 PrintString → 打印字符串），精确匹配失败时用 _NODE_TITLE_I18N 映射表
    双向变体回退（与 _find_node_by_title_i18n 同一映射表）。
    """
    try:
        # 精确匹配候选 + i18n 映射表双向变体（英文→中文、中文→英文）
        candidates = {node_id}
        for eng, variants in _NODE_TITLE_I18N.items():
            if eng == node_id:
                candidates.update(variants)
            elif node_id in variants:
                candidates.add(eng)
                candidates.update(variants)
        for node_info in unreal.BlueprintPythonBridge.list_nodes(graph):
            try:
                node_title = str(node_info.node_title)
            except Exception:
                continue
            if node_title in candidates:
                return node_info
    except Exception:
        return None
    return None


def _pin_name(p):
    """提取 pin 对象名称（兼容真实 UE EdGraphPin 与 fake FakePin）。"""
    try:
        return str(p.pin_name)
    except Exception:
        try:
            return str(p.get_name())
        except Exception:
            return ""



def _safe_call(fn, *args, error_cat: str = "unknown",
               pitfall_id: str = "", **kwargs):
    """安全封装 UE API 调用，统一异常转 BridgeError。

    红灯规则（来自 KB 踩坑记录）：
      - 禁止在 CDO 构造前访问属性
      - 跨 ue_send 调用引用可能丢失 → 用路径而非引用
      - 每次操作后验证结果
    """
    try:
        result = _gt_run_handler(fn, *args, timeout=300.0, **kwargs)
        return result
    except Exception as e:
        # T-20260909-SKILL-DEFECT-FIX：fn 可能没有 __name__（functools.partial /
        # MagicMock / 部分 C 扩展可调用对象）→ 直接访问抛 AttributeError，
        # 而异常发生在 except 块内 → 掩盖原始异常 + 丢失 error_cat 分类。
        _fn_name = getattr(fn, "__name__", None) or type(fn).__name__
        msg = f"[{error_cat}] {_fn_name}: {e}"
        raise BridgeError(msg, category=error_cat,
                          pitfall_id=pitfall_id,
                          suggestion=traceback.format_exc()[-300:]) from e


# ── GameThread 同步调度（Queue + 持久 tick worker）────

# 🔧 T-20260723-QUEUEFIX：替代每请求 register/unregister callback，
#                          改为启动时注册一个持久 tick worker + queue.Queue。
#   根因：uvicorn 线程调 register_slate_pre_tick_callback → SlateApplication.h:255
#         IsInGameThread() 断言 → UE 直接崩溃。
_GT_QUEUE = _queue.Queue()
_GT_RESULTS = {}          # task_id → {"result": ..., "error": ..., "event": threading.Event}
_GT_LOCK = threading.Lock()
_GT_COUNTER = 0
# 🔧 T-20260805-CRASH-ATEXIT-FIX P0-⑤：跨 reload 持久化（执剑者审计 BLOCK）
#   CPython reload 在「同一模块对象」上重新执行代码（不创建新对象），reload 会
#   重置所有顶层赋值，导致已注册的 Slate 回调 handle 丢失 → reload 后 start()
#   再注册新回调 → Slate 上旧+新双回调并存 → 旧回调访问半卸载引用 → AV。
#   修复：从旧模块属性恢复持久值（getattr 基于 reload 不重建对象的特性）：
#     - reload 前 _GT_WORKER_HANDLE=X → reload 后仍 = X（旧回调仍注册在 Slate）
#     - _GT_REGISTERED_CB 记录注册时的回调函数对象，用于区分「正常重复调用」
#       （handle 非 None 且回调=当前 _gt_worker → return）与「reload 旧回调残留」
#       （回调≠当前 _gt_worker → 先注销旧 handle 再注册新回调）
_GT_WORKER_HANDLE = getattr(sys.modules.get(__name__), '_GT_WORKER_HANDLE', None)
_GT_REGISTERED_CB = getattr(sys.modules.get(__name__), '_GT_REGISTERED_CB', None)
_GT_WORKER_STOPPED = getattr(sys.modules.get(__name__), '_GT_WORKER_STOPPED', False)


_GT_TASK_RUNNING = False  # GT worker 正在执行任务（嵌套分流安全阀）


def _gt_worker(delta_time: float):
    """持久的 GameThread tick worker——每帧从队列取一个任务执行。"""
    global _GT_WORKER_STOPPED, _GT_TASK_RUNNING
    # 🔧 T-20260805-CRASH-ATEXIT-FIX P0-③：已停止（stop()/UE 关闭）后，即使
    #   unregister 失败导致 Slate 残留回调被调用，也立即跳过——不取任务、不碰
    #   任何 unreal 对象（防 use-after-free AV）。
    if _GT_WORKER_STOPPED:
        return True  # 已停止：直接空转，不再执行任何队列任务
    try:
        task_id, fn, args, kwargs = _GT_QUEUE.get_nowait()
    except _queue.Empty:
        return True  # 继续等待下一帧

    _GT_TASK_RUNNING = True
    try:
        result = fn(*args, **kwargs)
        with _GT_LOCK:
            entry = _GT_RESULTS.get(task_id)
            if entry:
                entry["result"] = result
                entry["event"].set()
    except Exception as e:
        with _GT_LOCK:
            entry = _GT_RESULTS.get(task_id)
            if entry:
                # 🔧 D: 跨线程异常剥离（T-20260806-UE5BRIDGE-CRASH-FIX 方案D）
                #   只把「字符串化」的错误传给请求线程，绝不传原始异常对象——
                #   原始异常 __traceback__ 帧持有 GT 线程的 unreal 局部变量
                #   （bp/graph/node_info/k2），跨线程持有后 asyncio/线程池线程
                #   后续 GC/序列化访问 → Python 内存损坏（0x6e656b6f8c 家族）。
                entry["error"] = BridgeError(
                    f"{type(e).__name__}: {e}",
                    category="game_thread",
                    suggestion=traceback.format_exc()[-500:],
                )
                entry["event"].set()
    finally:
        _GT_TASK_RUNNING = False
    return True  # 持续运行，不注销


def _gt_start_worker():
    """启动持久的 GameThread tick worker（仅注册一个；reload 后自动清理旧回调）。"""
    global _GT_WORKER_HANDLE, _GT_REGISTERED_CB, _GT_WORKER_STOPPED
    # 正常重复调用（未 reload）：handle 非空且注册的回调仍是当前 _gt_worker → 不重复注册
    if _GT_WORKER_HANDLE is not None and _GT_REGISTERED_CB is _gt_worker:
        return
    # 🔧 T-20260805-CRASH-ATEXIT-FIX P0-⑤：reload 后旧回调仍注册在 Slate
    #   （handle 通过模块属性持久化保留）→ 先注销旧 handle，再注册当前模块的回调，
    #   保证 Slate 上始终只有一个 tick worker（防双回调 use-after-free）。
    if _GT_WORKER_HANDLE is not None:
        try:
            unreal.unregister_slate_pre_tick_callback(_GT_WORKER_HANDLE)
        except Exception as _e:
            _OP_LOG.warning(f"注销旧 GameThread tick worker 失败: {_e}")
        _GT_WORKER_HANDLE = None
    try:
        _GT_WORKER_HANDLE = unreal.register_slate_pre_tick_callback(_gt_worker)
        _GT_REGISTERED_CB = _gt_worker
        _GT_WORKER_STOPPED = False
        _OP_LOG.info("GameThread tick worker 已启动")
    except AttributeError:
        raise BridgeError(
            "unreal.register_slate_pre_tick_callback 不可用（UE 版本 < 5.0？）",
            category="game_thread",
        )


def _gt_stop_worker():
    """停止 GameThread tick worker。"""
    global _GT_WORKER_HANDLE, _GT_REGISTERED_CB, _GT_WORKER_STOPPED
    if _GT_WORKER_HANDLE is not None:
        try:
            unreal.unregister_slate_pre_tick_callback(_GT_WORKER_HANDLE)
        except Exception as _e:
            _OP_LOG.warning(f"注销 GameThread tick worker 失败: {_e}")
        _GT_WORKER_HANDLE = None
    _GT_REGISTERED_CB = None
    _GT_WORKER_STOPPED = True


def _drain_gt_queue():
    """排空 GameThread 队列（T-20260805-CRASH-ATEXIT-FIX P0-③）。

    UE 关闭/stop() 期间调用：
      - _GT_QUEUE：清空残留待执行任务（shutdown 阶段不允许 worker 再执行
        任何 unreal 操作——访问半卸载对象 → AV）。
      - _GT_RESULTS：对尚未完成的条目置 error + set event，唤醒阻塞在
        _run_on_game_thread_sync 的调用方（防止挂死），随后清空。
    """
    # 1. 清空待执行任务队列
    while True:
        try:
            _GT_QUEUE.get_nowait()
        except _queue.Empty:
            break
    # 2. 唤醒并清理未完成结果
    with _GT_LOCK:
        for _tid, _entry in list(_GT_RESULTS.items()):
            if not _entry["event"].is_set():
                _entry["error"] = BridgeError(
                    "服务已停止，任务未执行",
                    category="game_thread",
                    suggestion="服务已停止，请重新 start() 后重试",
                )
                _entry["event"].set()
        _GT_RESULTS.clear()


def _run_on_game_thread_sync(fn, *args, timeout: float = 30.0, **kwargs):
    """在 GameThread 上同步执行 fn(*args, **kwargs) 并返回结果。

    实现（T-20260723-QUEUEFIX 重构）：
      uvicorn 线程 → queue.Queue 投递任务 → 持久的 tick worker
      在 GameThread 取出并执行 → threading.Event 通知完成。

    优势：
      - 只注册一次 tick callback，uvicorn 线程永不碰 UE API
      - 避免 register/unregister 的 IsInGameThread 崩溃
      - 支持并发请求（每请求独立 task_id + Event）
    """
    global _GT_COUNTER
    # 🔧 v3.2 实测修复：重入保护——已在 GT 任务内（_gt_worker 正在执行本线程）
    #    时再入队会自等待（worker 阻塞在本次调用上，永远取不到新任务）→ 30s 级联超时。
    #    此时必然已在 GameThread 上 → 直接内联执行。
    if _GT_TASK_RUNNING:
        return fn(*args, **kwargs)
    with _GT_LOCK:
        _GT_COUNTER += 1
        task_id = _GT_COUNTER
        event = threading.Event()
        _GT_RESULTS[task_id] = {"result": None, "error": None, "event": event}

    _GT_QUEUE.put((task_id, fn, args, kwargs))

    if not event.wait(timeout=timeout):
        with _GT_LOCK:
            _GT_RESULTS.pop(task_id, None)
        raise BridgeError(
            f"GameThread 操作超时 ({timeout}s): {fn.__name__}",
            category="game_thread",
            suggestion="UE 编辑器可能卡住，检查是否有模态对话框"
        )

    with _GT_LOCK:
        entry = _GT_RESULTS.pop(task_id, None)

    if entry and entry["error"]:
        raise entry["error"]
    return entry["result"] if entry else None


def _gt_exec_python(code: str, timeout: float = 60.0):
    """在 GameThread 上执行生成的 python 代码（T-20261025-VERIFY-FIX）。

    旧实现调用 C++ 桥的 ExecutePythonOnGameThread（其内部 AsyncTask 入 GT）；
    5.8 起 UFUNCTION 从非 GT 线程调用被引擎直接拒绝（B8 同源——9 月旧 DLL 无
    此断言所以未暴露，新 DLL 一打即中）→ 改由本模块 GT 队列直呼 exec
    （同 pie_start 通道），"代码在 GT 执行"的语义不变，C++ 跳板退役。
    调用方的 result 文件轮询协议保持不变。
    """
    if _TEST_MODE:
        # 离线测试：保持旧 C++ 跳板调用 —— bridge 是 fake/mock，D8 契约测试
        # 的 mock 会在其中同步执行代码（写入 result 文件），不能 no-op
        if BRIDGE_AVAILABLE and hasattr(bridge, "execute_python_on_game_thread"):
            bridge.execute_python_on_game_thread(code)
        return
    if _GT_TASK_RUNNING:
        exec(code, {"__name__": "__ue5_gt_exec__"})  # 已在 GT（嵌套调用）→ 直呼
        return
    _run_on_game_thread_sync(
        lambda: exec(code, {"__name__": "__ue5_gt_exec__"}), timeout=timeout)


# ══════════════════════════════════════════════════════════
#  编译状态读取（D-2 · T-20260913-UE5BRIDGE-DELETE-COMPILE-FIX）
# ══════════════════════════════════════════════════════════
#
# 🔴 编码前 KB 语义搜索（kb=agent_knowledge）：
#    「UE蓝图API静默失败防范」[已验证] —— 「返回值说谎」是最难排查的失败模式。
#    本模块统一口径：状态读不到 → 确定字符串 'unknown'，
#    ⛔ 绝不返回 '' / None / 键缺失（D-2 判据恰恰 = 回读 null/空）。
#
# 🔴 UE5.1.1 **在线实测**（2026-09-13 · dev-project · PID 77252 · 脚本 p_status.py）：
#    · `unreal.EBlueprintStatus`            —— **不存在**（hasattr=False）
#    · `bp.get_editor_property('status')`   —— **不可读**：
#        "Blueprint: Property 'Status' for attribute 'status' on 'Blueprint'
#         is protected and cannot be read"
#    · `bp.status`                          —— **无此属性**（dir(bp) 零 status 属性）
#    · C++ bridge                           —— 仅 CompileBlueprint/CompileAndSave
#                                              （均返回 bool）· **无状态读法**
#    ∴ 引擎侧状态在本版 UE **不可直接读取** → 读法优先级（实测驱动）：
#      ① get_editor_property('status')   ② getattr(bp, 'status')
#      ③ C++ bridge（将来若暴露 get_blueprint_status 之类 → 自动启用）
#      ④ **由编译动作结果派生**（compiled True → BS_UpToDate / False → BS_Error）
#      ⑤ 'unknown'
_BP_STATUS_BY_INDEX = {
    0: "BS_Unknown", 1: "BS_Dirty", 2: "BS_Error",
    3: "BS_UpToDate", 4: "BS_UpToDateWithWarnings", 5: "BS_BeingCreated",
}
_BP_STATUS_BY_NAME = {
    "UPTODATEWITHWARNINGS": "BS_UpToDateWithWarnings",
    "UPTODATE": "BS_UpToDate",
    "DIRTY": "BS_Dirty",
    "ERROR": "BS_Error",
    "BEINGCREATED": "BS_BeingCreated",
    "UNKNOWN": "BS_Unknown",
}
#: ⚠️ 顺序敏感：长名必须先判（UPTODATEWITHWARNINGS 在 UPTODATE 之前）
_BP_STATUS_NAMES = ("UPTODATEWITHWARNINGS", "UPTODATE", "DIRTY", "ERROR",
                    "BEINGCREATED", "UNKNOWN")


def _bp_status_normalize(value):
    """引擎状态值（int / 枚举 / str）→ 确定字符串；不可解析 → None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        idx = int(value)
    except (TypeError, ValueError):
        idx = None
    if idx is not None:
        return _BP_STATUS_BY_INDEX.get(idx)
    text = str(value)
    if not text:
        return None
    tail = text.rsplit(".", 1)[-1].upper().replace("_", "")
    for name in _BP_STATUS_NAMES:
        if tail.endswith(name):
            return _BP_STATUS_BY_NAME[name]
    return None


def _bp_status_str(bp, compiled=None):
    """读蓝图编译状态 → 确定字符串（⛔ 永不返回 '' / None）。

    Args:
        bp: 蓝图对象（必须在 GameThread 上访问）。
        compiled: 本次编译动作结果 True/False/None（引擎状态不可读时用于派生）。

    Returns:
        "BS_UpToDate" / "BS_Error" / "BS_Dirty" / "BS_UpToDateWithWarnings" /
        "BS_BeingCreated" / "BS_Unknown" / "unknown"（兜底，确定非空）。
    """
    for _getter in (lambda: bp.get_editor_property("status"),
                    lambda: getattr(bp, "status", None)):
        try:
            _norm = _bp_status_normalize(_getter())
        except Exception:
            _norm = None
        if _norm:
            return _norm
    # ③ C++ bridge（若将来插件暴露状态读法 → 自动启用，无需改本函数）
    try:
        if BRIDGE_AVAILABLE:
            for _name in ("get_blueprint_status", "blueprint_status",
                          "get_compile_status"):
                _fn = getattr(bridge, _name, None)
                if callable(_fn):
                    _norm = _bp_status_normalize(_fn(bp))
                    if _norm:
                        return _norm
    except Exception:
        pass
    # ④ 由编译动作结果派生（确定性）
    if compiled is True:
        return "BS_UpToDate"
    if compiled is False:
        return "BS_Error"
    # ⑤ 兜底：确定字符串
    return "unknown"


def _bp_status_str_gt(bp, compiled=None):
    """GameThread 包装：uvicorn 线程不得直接访问 UE 对象。"""
    return _run_on_game_thread_sync(
        lambda: _bp_status_str(bp, compiled=compiled), timeout=15.0)


def _safe_json(data: Any) -> str:
    """安全序列化为 JSON，处理 unreal 对象。"""
    class UEEncoder(json.JSONEncoder):
        def default(self, obj):
            # unreal 类型 → 字符串
            if hasattr(obj, 'get_name'):
                return str(obj.get_name())
            if hasattr(obj, 'get_path_name'):
                return str(obj.get_path_name())
            try:
                return str(obj)
            except:
                return f"<unreal:{type(obj).__name__}>"
    return json.dumps(data, cls=UEEncoder, ensure_ascii=False, indent=2)


def _resolve_blueprint(blueprint_path: str):
    """通过资产路径加载蓝图。

    红灯规则（KB: P08 CROSS_CALL_REF_LOSS）：
      每次操作都重新加载蓝图，不缓存引用。

    线程安全性：
      bridge.load_blueprint_asset() 内部用 AsyncTask 调度到 GameThread，
      因此在 uvicorn 子线程中也能安全调用。如果 Bridge 不可用，降级为
      _run_on_game_thread_sync 包裹 unreal.EditorAssetLibrary.load_asset。
    """
    if not blueprint_path.startswith("/Game/"):
        blueprint_path = f"/Game/{blueprint_path}"

    # 优先：C++ Bridge 的线程安全加载
    if BRIDGE_AVAILABLE and hasattr(bridge, 'load_blueprint_asset'):
        try:
            bp = bridge.load_blueprint_asset(blueprint_path)
            if bp:
                return bp
        except Exception as e:
            _OP_LOG.warning(f"bridge.load_blueprint_asset 失败，降级: {e}")

    # 降级：GameThread 同步调度包裹 load_asset
    try:
        bp = _run_on_game_thread_sync(
            unreal.EditorAssetLibrary.load_asset,
            blueprint_path,
            timeout=15.0  # load_asset 通常很快，15s 足够
        )
    except BridgeError as e:
        if "GameThread 操作超时" in str(e):
            raise BridgeError(
                f"加载蓝图超时: {blueprint_path}",
                category="asset_load",
                suggestion="检查蓝图路径是否正确，UE 编辑器是否响应"
            )
        raise

    if not bp:
        raise BridgeError(
            f"蓝图不存在: {blueprint_path}",
            category="asset_load"
        )
    return bp


def _get_ue_info() -> Dict[str, str]:
    """获取 UE 编辑器和项目信息。

    ⚠️ unreal.* 必须在 GameThread 调（真机实录 2026-10-24：HTTP 线程上连
    get_engine_version 都抛异常 → 旧实现恒返回 unknown）—— 统一走 GT 队列
    （同 pie_start 口径；TEST_MODE 由 GT 机制既有分支覆盖）。任何失败落
    "unknown"，/health 绝不因元数据挂掉。
    """
    def _collect():
        info = {}
        try:
            info["engine_version"] = str(unreal.SystemLibrary.get_engine_version())
        except Exception:
            info["engine_version"] = "unknown"
        try:
            info["project_name"] = unreal.SystemLibrary.get_game_name()
        except Exception:
            info["project_name"] = "unknown"
        try:
            # unreal.Paths.* 恒返回相对路径 —— 转绝对路径，监控/日志才可定位。
            info["project_dir"] = str(unreal.Paths.convert_relative_path_to_full(
                unreal.Paths.project_dir()))
        except Exception:
            info["project_dir"] = "unknown"
        info["bridge_loaded"] = BRIDGE_AVAILABLE
        return info

    try:
        return _run_on_game_thread_sync(_collect, timeout=5.0)
    except Exception:
        return {"engine_version": "unknown", "project_name": "unknown",
                "project_dir": "unknown", "bridge_loaded": BRIDGE_AVAILABLE}


# ══════════════════════════════════════════════════════════
#  GET /health — 连通性检查
# ══════════════════════════════════════════════════════════

@app.get("/health")
def health_check(request: Request = None):  # 🔧 B: async→sync def（T-20260806-UE5BRIDGE-CRASH-FIX 方案B·线程池隔离）
    """Bridge 连通性 + UE 版本 + 项目路径。

    Returns:
        {
            "status": "ok",
            "bridge_version": "0.5.0",
            "ue_info": {...},
            "uptime_seconds": 123,
            "request_count": 456
        }
    """
    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")

    global _request_count
    with _lock:
        _request_count += 1

    ue_info = _get_ue_info()
    uptime = int(time.time() - _start_time)
    return {
        "status": "ok",
        "bridge_version": "0.5.0",
        "ue_info": ue_info,
        "uptime_seconds": uptime,
        "request_count": _request_count,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ══════════════════════════════════════════════════════════
#  GET /diag — Bridge API 诊断
# ══════════════════════════════════════════════════════════

@app.get("/diag")
def bridge_diag(request: Request = None):  # 🔧 B: async→sync def（T-20260806-UE5BRIDGE-CRASH-FIX 方案B·线程池隔离）
    """返回 Bridge DLL 暴露的 API 清单和可用性。

    用于自动化脚本在调用前确认所需 API 是否存在，
    避免运行时 AttributeError。
    """
    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")

    global _request_count
    with _lock:
        _request_count += 1

    diag = {
        "bridge_available": BRIDGE_AVAILABLE,
        "bridge_class": str(type(bridge)) if BRIDGE_AVAILABLE else "N/A",
    }

    if BRIDGE_AVAILABLE:
        all_methods = sorted([m for m in dir(bridge) if not m.startswith("_")])
        diag["all_methods"] = all_methods
        diag["method_count"] = len(all_methods)

        # 关键 API 可用性矩阵
        key_apis = [
            "add_event_node", "add_function_call_node", "add_function_node",
            "find_node_by_title", "connect_pins", "list_pins",
            "get_node_connections", "get_pin_infos", "set_pin_default_value",
            "get_event_graph", "get_all_graphs", "list_nodes",
            "create_actor_blueprint", "add_member_variable", "compile_blueprint",
            "load_blueprint_asset", "get_version", "execute_python_on_game_thread",
        ]
        diag["key_api_matrix"] = {
            api: hasattr(bridge, api) for api in key_apis
        }
        diag["missing_apis"] = [
            api for api in key_apis if not hasattr(bridge, api)
        ]
    else:
        diag["all_methods"] = []
        diag["method_count"] = 0
        diag["key_api_matrix"] = {}
        diag["missing_apis"] = ["<bridge not loaded>"]

    return diag


# ══════════════════════════════════════════════════════════
#  POST /command — 白名单命令模式（A-003 + A-004 安全升级）
# ══════════════════════════════════════════════════════════


class CommandRequest(BaseModel):
    command: str
    params: dict = {}


class CommandResponse(BaseModel):
    success: bool
    command: str = ""
    result: Any = None
    error: str = ""
    duration_ms: float = 0.0


# ── 白名单命令实现 ──────────────────────────────────────

def _cmd_list_assets(path: str = "/Game/", recursive: bool = True):
    """列出指定路径下的资产。

    T-20260914-UE5BRIDGE-READPATH-FIX 补丁② · D-6：
      `unreal.EditorAssetLibrary.list_assets` 是 **GT-only API** —— 由
      uvicorn 线程直调**恒返空**（静默失败、无异常，最难排查的失败模式）。
      修法：**复用既有 GT 调度入口** `_run_on_game_thread_sync`（与
      `_cmd_save_asset`(L3776) / `_cmd_get_pin_default_value`(L1373) 同型），
      在 GameThread 内执行。⛔ 未新造机制。
      ⚠️ 本函数由 uvicorn 线程调用（`/command` 白名单 L4182）→ 必须**入队**；
      不得直调（对比：PATH2 注入脚本已在 GT 内 → 只能直调 _impl，见 D-5）。
    """
    def _do():
        return _safe_call(
            unreal.EditorAssetLibrary.list_assets,
            path, recursive=recursive,
            error_cat="list_assets"
        )

    result = _run_on_game_thread_sync(_do, timeout=30.0)
    return [str(r) for r in result] if result else []


def _cmd_read_blueprint(bp_path: str, level: str = "L1",
                         include_cdo: bool = True,
                         include_conn: bool = True) -> dict:
    """复用 /read 逻辑：L1 信息 + 变量 + CDO + L2 节点。"""
    bp = _resolve_blueprint(bp_path)
    result = {
        "blueprint_path": bp_path,
        "level": level,
        "l1": _read_l1_info(bp),
        "variables": _read_variables(bp),
    }
    if include_cdo:
        result["cdo"] = _read_cdo(bp)
    if level in ("L2", "full") and BRIDGE_AVAILABLE:
        result["l2"] = _read_l2_nodes(bp, include_connections=include_conn)
    elif level in ("L2", "full") and not BRIDGE_AVAILABLE:
        result["l2"] = {"available": False,
                         "error": "C++ Bridge 插件未加载"}
    return result


def _cmd_read_variables(bp_path: str):
    """只读变量清单。"""
    bp = _resolve_blueprint(bp_path)
    return _read_variables(bp)


def _cmd_read_cdo(bp_path: str):
    """只读 CDO。"""
    bp = _resolve_blueprint(bp_path)
    return _read_cdo(bp)


def _cmd_read_nodes(bp_path: str, graph_name: str = None):
    """只读节点。

    `graph_name` 语义（T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE-R3 收口 · 原 §17.3 待裁项
    → maintainer授权「已裁决 + 已实施」）：
      · **有值**（如 "EventGraph" / "UserConstructionScript"）→ `graphs[]` **只返回该图**；
      · **未指定**（None / 空串）→ **保持全量**（向后兼容依赖「一次拿全图」的调用方）。

    修复前缺陷（raw\\c2_nodes.json 实测）：入参 `graph_name="EventGraph"` 时 `graphs[]`
    仍带回 `UserConstructionScript` / `EdGraph_0` / `MathExpression` 等其它图。
    """
    bp = _resolve_blueprint(bp_path)
    l2 = _read_l2_nodes(bp, include_connections=False)
    want = (graph_name or "").strip()
    if want:
        l2["graphs"] = [g for g in l2.get("graphs", [])
                        if g.get("graph_name") == want]
    return l2


def _cmd_read_connections(bp_path: str, graph_name: str = "EventGraph"):
    """只读连线。"""
    bp = _resolve_blueprint(bp_path)
    l2 = _read_l2_nodes(bp, include_connections=True)
    for g in l2.get("graphs", []):
        if graph_name == "EventGraph" or g.get("graph_name") == graph_name:
            g.pop("nodes", None)
    return l2


def _cmd_build_blueprint(bp_name: str, pkg: str = "/Game/Blueprints/",
                          parent: str = "Actor",
                          vars=None,
                          comps=None,
                          interfaces=None,
                          compile_after: bool = True) -> dict:
    """复用 /build 逻辑：创建蓝图 + 变量 + 组件 + 接口 + 编译。"""
    print("[ue5_bridge] [INFO] /command build_blueprint 兼容模式（仍可用），新代码请使用 POST /build（T-20260805-DOCFIX）")
    bp = None
    steps = []
    try:
        # ── 创建蓝图资产 ──
        full_path = f"{pkg}{bp_name}"
        if not _safe_game_pkg(full_path):
            raise BridgeError("build_blueprint 只允许规范 /Game/ 路径（收到 %r）"
                              % full_path, category="param_type")
        existing = _run_on_game_thread_sync(unreal.EditorAssetLibrary.does_asset_exist, full_path)
        if existing:
            raise BridgeError(
                f"蓝图已存在: {full_path}",
                category="asset_create"
            )

        if BRIDGE_AVAILABLE:  # T-20260723-BYPASS-DONE: DLL rebuilt with ExecutePythonOnGameThread
            bp = _safe_call(
                bridge.create_actor_blueprint, pkg, bp_name,
                error_cat="asset_create", pitfall_id="P07"
            )
        else:
            factory = unreal.BlueprintFactory()
            factory.set_editor_property("parent_class",
                unreal.load_class(None, f"/Script/Engine.{parent}"))
            bp = _safe_call(
                unreal.AssetToolsHelpers.get_asset_tools().create_asset,
                bp_name, pkg.rstrip("/"), None, factory,
                error_cat="asset_create"
            )
        steps.append({
            "step": "create_asset", "status": "ok",
            "asset_path": str(bp.get_path_name()) if bp else "unknown"
        })
        # v5.0：记入任务资产账本（rollback_task_assets 据此删除新建资产）
        if _TASK_JOURNAL_AVAILABLE and _task_journal is not None:
            try:
                _task_journal.note_created_asset(full_path)
            except Exception:
                pass

        # ── 添加变量（T-20260909-USER-DRILL：写后回读验证）──
        if vars:
            for var_def in vars:
                var_name = var_def.get("name", "")
                var_type = var_def.get("type", "bool")
                _add_variable_verified(bp, var_name, var_type, var_def, steps)

        # ── 添加组件 ──
        if comps:
            for comp_def in comps:
                comp_name = comp_def.get("name", "")
                comp_class = comp_def.get("component_class", "SceneComponent")
                # T-20260909-SKILL-DEFECT-FIX：同 POST /build，走 C++ SCS 优先
                _add_component_preferred(
                    bp, comp_name, comp_class, comp_def.get("properties", {}))
                steps.append({
                    "step": f"add_component:{comp_name}", "status": "ok"
                })

        # ── 添加接口 ──
        if interfaces:
            for iface in interfaces:
                _safe_call(
                    _add_interface_unreal, bp, iface,
                    error_cat="interface_add"
                )
                steps.append({
                    "step": f"add_interface:{iface}", "status": "ok"
                })

        # ── 编译 ──
        if compile_after:
            if BRIDGE_AVAILABLE:  # T-20260723-BYPASS2-DONE: DLL rebuilt with ExecutePythonOnGameThread
                ok = _safe_call(
                    bridge.compile_blueprint, bp,
                    error_cat="compile"
                )
            else:
                ok = _safe_call(
                    unreal.BlueprintEditorLibrary.compile_blueprint, bp,
                    error_cat="compile"
                )
            steps.append({
                "step": "compile", "status": "ok" if ok else "failed"
            })
            if not ok:
                return {"success": False, "steps": steps,
                        "error": "蓝图编译失败"}

        return {"success": True, "steps": steps}
    except BridgeError as e:
        return {"success": False, "steps": steps,
                "error": str(e), "category": e.category}


def _cmd_add_variable(bp_path: str, var_name: str, var_type: str,
                       default_value: str = "", category: str = "",
                       expose: bool = True) -> dict:
    """给已有蓝图添加变量。"""
    bp = _resolve_blueprint(bp_path)
    var_def = {
        "name": var_name, "type": var_type,
        "default_value": default_value,
        "category": category, "expose": expose
    }
    _steps: list = []
    if not _add_variable_verified(bp, var_name, var_type, var_def, _steps):
        raise BridgeError(
            _steps[-1]["error"], category="variable_add",
            suggestion="实测可用类型：bool / int / float / string / vector")
    return {"added": var_name, "type": var_type}


def _cmd_add_component(bp_path: str, comp_name: str, comp_class: str,
                        properties=None) -> dict:
    """给已有蓝图添加组件。"""
    bp = _resolve_blueprint(bp_path)
    _add_component_preferred(bp, comp_name, comp_class, properties)
    return {"added": comp_name, "class": comp_class}


def _add_component_preferred(bp, comp_name: str, comp_class: str,
                             properties=None):
    """添加组件（C++ SCS API 优先，legacy unreal API 兜底）。

    T-20260909-SKILL-DEFECT-FIX：抽出公共实现。此前 `/command add_component`
    走 C++ 路径（v0.9+，2026-08-23 在线验证），而 `POST /build` 与
    `_cmd_build_blueprint`（兼容模式）仍走 legacy `_add_component_unreal`
    ——后者依赖 UE5.1 未向 Python 暴露的 SimpleConstructionScript
    （KB: UE5_Python_API_组件添加限制_SCS未暴露.md）→ **创建模式带组件必然
    失败**。离线回归 `tests/test_build_blueprint.py` 已复现「无法获取 SCS」。

    返回：成功 None；失败抛 BridgeError（category=component_add）。
    """
    if BRIDGE_AVAILABLE and hasattr(bridge, 'add_component_to_blueprint'):
        # T-20260822-ADDCOMPONENT-CPP：C++ SCS API 优先（v0.9 新增，
        # 绕过 UE5.1 Python 无 SimpleConstructionScript 暴露限制，
        # 见 KB UE5_Python_API_组件添加限制_SCS未暴露.md [已验证]）。
        # T-20260823-UE5BRIDGE-CPP-APIS 子项1：properties 不再 fail-loud——
        # 添加成功后逐属性调用 C++ SetComponentProperty（v0.10 新增），
        # 取代旧路径（BPGC.ComponentTemplates 不可达 → 静默失效，裁决 C
        # 过渡 fail-loud 已由真实现取代）。
        result = _safe_call(
            bridge.add_component_to_blueprint, bp, comp_name, comp_class,
            error_cat="component_add"
        )
        # 真实 UE 反射语义（T-20260822-ADDCOMPONENT-ONLINE 阶段1实测 +
        # T-20260822-ADDCOMPONENT-RENAME-ERR 引擎源码实锤 PyGenUtil.cpp
        # PackReturnValues）：
        #   C++ 签名 (bool, FString& OutError) → UE Python 反射规则：
        #     bool=false → Python 返回 None（OutError 被吞，无论是否赋值）
        #     bool=true  → Python 返回 OutError 值（成功 '' / 非空错误文本）
        #   → 返回 str or None：
        #     '' 空字符串 = 成功 / None = 失败（bool=false 类，OutError 不可达）/
        #     非空 str = 失败且值为错误文本
        #   重名分支（裁决 B 严格拒绝）C++ 侧 return true + OutError 非空，
        #   正是为了让绑定传出错误文本（见 BlueprintPythonBridge.cpp ②.5）。
        #   （旧 bool(result) 解析把 '' → False 误判为失败，已废弃；
        #    tuple/scalar 元组假设与真实行为不符，已删除）
        if result is None or result is False:
            raise BridgeError(
                f"组件添加失败: {comp_class}", category="component_add")
        if isinstance(result, str) and result:
            raise BridgeError(
                f"组件添加失败: {result}", category="component_add")
        # 其余（'' 空字符串 / True）视为成功

        # T-20260823-UE5BRIDGE-CPP-APIS 子项1：添加成功后逐属性设置（C++ API）
        if properties:
            _apply_component_properties(bp, comp_name, properties)
    else:
        _safe_call(
            _add_component_unreal, bp, comp_name, comp_class,
            properties or {},
            error_cat="component_add"
        )


def _component_property_value_to_string(val) -> str:
    """把 Python 属性值转为 C++ SetComponentProperty 接受的字符串。

    T-20260823-UE5BRIDGE-CPP-APIS 子项1：
      - bool → "true"/"false"（ImportText 可解析）
      - int/float → str（ImportText 可解析）
      - list/tuple → 空格分隔数值（FVector/FRotator："100 100 100"）
      - dict → "Key=Value ..."（struct 属性：FLinearColor 等）
      - str → 原样
    """
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, (list, tuple)):
        return " ".join(str(v) for v in val)
    if isinstance(val, dict):
        return " ".join(f"{k}={v}" for k, v in val.items())
    return str(val)


def _apply_component_properties(bp, comp_name: str, properties: Dict):
    """C++ 路径：添加组件后逐个调用 bridge.set_component_property 设置属性。

    T-20260823-UE5BRIDGE-CPP-APIS 子项1：取代 fail-loud 拦截（裁决 C 过渡）。
    对齐裁决 A 语义（真实 UE 反射 bool+FString& 规则，同 _cmd_add_component）：
      ''=成功 / None=失败无文本 / 非空 str=失败+错误文本
    """
    if not properties:
        return
    if not (BRIDGE_AVAILABLE and hasattr(bridge, 'set_component_property')):
        raise BridgeError(
            "C++ Bridge 无 set_component_property API，无法设置组件属性",
            category="component_add",
        )
    for prop_name, prop_val in properties.items():
        value_str = _component_property_value_to_string(prop_val)
        result = _safe_call(
            bridge.set_component_property, bp, comp_name, prop_name, value_str,
            error_cat="component_add",
        )
        if result is None or result is False:
            raise BridgeError(
                f"设置组件属性失败: {comp_name}.{prop_name}",
                category="component_add",
            )
        if isinstance(result, str) and result:
            raise BridgeError(
                f"设置组件属性失败: {result}", category="component_add")


def _function_name_candidates(function_name: str) -> List[str]:
    """函数名候选（诊断用）：原名 / 去 `K2_` 前缀 / 加 `K2_` 前缀（去重保序）。

    T-20260916-UE5AUTO-HALLBEACON-D1D4-FIX · D-3 新增：`add_function_node` 返回
    None 时**必须**给出可读候选，禁止裸 `"returned None"`。
    依据（P5 在线实测）：UE5.1《AActor》真实函数名是 `SetActorHiddenInGame`，
    `K2_` 前缀仅用于**重载冲突**函数（如 `K2_SetActorLocation`）→ 传
    `K2_SetActorHiddenInGame` 必然 `FindFunction` 失败。
    """
    s = str(function_name or "").strip()
    if not s:
        return []
    out = [s]
    alt = s[3:] if s.startswith("K2_") else ("K2_" + s)
    if alt and alt not in out:
        out.append(alt)
    return out



def _cmd_add_function_node(bp_path: str, graph_name: str,
                            function_path: str,
                            node_pos=None) -> dict:
    """给蓝图图表添加函数节点。

    返回体契约（D-6 · T-20260917-D5D6-CONTRACT-FIX）：
        `{"added_node": <节点标题>, "node_id": <32 位 hex GUID>, "graph": ...,
          "warnings": [...]}`
      · `node_id` —— **新增**，可直接喂 `set_pin_default_value` 的 `node_id` /
        `node_guid` 参数（同名节点场景的唯一可靠锚点）
      · `added_node` —— **保留**（向后兼容，不改名不删除）
    """
    bp = _resolve_blueprint(bp_path)
    if not BRIDGE_AVAILABLE:
        raise BridgeError("C++ Bridge 插件未加载", category="node_add")
    graph = bridge.get_event_graph(bp) if graph_name == "EventGraph" else None
    if not graph:
        raise BridgeError(f"图表未找到: {graph_name}", category="node_add")
    # T-20260820-UE5BRIDGE-API-GAP-FIX（A'）：
    #   DLL 无 add_function_node 方法，实际暴露
    #   add_function_call_node(graph, function_name, target_class_name,
    #   pos_x=0, pos_y=0)（C++ 侧 AddFunctionCallNode，UE 反射为 Python
    #   风格小写；评估报告 §1.2-1.3 实测 __doc__ 确认签名）。
    #   function_path 形如 /Script/Engine.KismetSystemLibrary:Delay →
    #   按最后一个 ':' 拆分为 target_class_name + function_name。
    target_class_name, _, function_name = function_path.rpartition(":")
    if not target_class_name or not function_name:
        raise BridgeError(
            f"function_path 格式错误（应为 类路径:函数名）: {function_path}",
            category="param_type",
        )
    pos_x = int(node_pos[0]) if node_pos else 0
    pos_y = int(node_pos[1]) if node_pos else 0
    node = _safe_call(
        bridge.add_function_call_node, graph, function_name,
        target_class_name, pos_x, pos_y,
        error_cat="node_add"
    )
    _alt_used_warning = ""
    # ── E-12（v2.20 · 候选名自动重试 + 显式告警）────────────────────────────
    #   实测（2026-09-18 dev-project）：GetActorLocation 在 UE 反射中不可解析
    #   （AActor 的蓝图暴露名是 K2_GetActorLocation，原函数非 UFUNCTION），
    #   D-3 的候选清单只提示不重试 → 开箱第一个纯函数节点就建不出。
    #   候选重试是**确定性名字归一**（同一函数的反射别名），非歧义猜测；
    #   换名成功时回传 warnings 显式记录（可审计，不静默）。
    if node is None:
        for _cand in _function_name_candidates(function_name)[1:]:
            node = _safe_call(
                bridge.add_function_call_node, graph, _cand,
                target_class_name, pos_x, pos_y,
                error_cat="node_add"
            )
            if node is not None:
                _alt_used_warning = (
                    "函数名 %r 在 %s 反射中不可解析 → 已自动改用候选 %r 建节点"
                    % (function_name, target_class_name, _cand))
                break
    # ── D-3（T-20260916-UE5AUTO-HALLBEACON-D1D4-FIX · fail-loud）───────────────
    #   旧实现：node is None 时照常返回
    #   `{"added_node": <function_path>, "graph": ...}` —— **假阳性成功**
    #   （调用方看到「成功」但节点并未建立）。P5 在线实测踩中：
    #   传 `/Script/Engine.Actor:K2_SetActorHiddenInGame` → DLL 返回 None
    #   （AActor 真实函数名为 SetActorHiddenInGame，K2_ 前缀仅用于重载冲突函数）。
    #   修法：None → **必抛 BridgeError**，附可读原因 + 函数名候选。
    if node is None:
        _cands = _function_name_candidates(function_name)
        raise BridgeError(
            "add_function_node returned None（DLL 未建出节点）: "
            "类=%s · 函数=%s · 函数名候选=%s；"
            "常见原因：① 函数名不存在（UE5.1 中 AActor 为 SetActorHiddenInGame，"
            "K2_ 前缀仅用于重载冲突函数）② 该类不含此函数 "
            "③ 非 UFUNCTION（缺 BlueprintCallable）"
            "④ target_class_name 拼写 / 大小写错误"
            % (target_class_name, function_name, _cands),
            category="node_add",
            suggestion="改用候选名之一；或先用 read_blueprint 回读目标类实际函数名",
        )
    if node_pos:
        try:
            node.pos_x, node.pos_y = node_pos[0], node_pos[1]
        except:
            pass
    # ── D-6（T-20260917-D5D6-CONTRACT-FIX · R6-1 ~ R6-4）───────────────────────
    #   缺陷史：返回体仅 `{"added_node": <title>, "graph": ...}` → 同名节点场景
    #   拿不到标识，调用方只能「回读全图 + 按坐标反查 GUID」绕路。
    #   修法：**新增** `node_id`（32 位 hex GUID，与 `_pywrap_node_result` /
    #   `_cmd_create_node_by_class` / `set_pin_default_value(node_id|node_guid)`
    #   全库既有口径一致）；`added_node` **保留**（R6-2 向后兼容，不改名不删除）。
    #   R6-3：GUID 取用现成原语 `_pywrap_node_guid`；R6-4：取不到 → 走回读兜底，
    #   仍取不到则 `node_id: ""` + `warnings` **显式标注原因**（绝不空串不吭声）。
    _guid = _pywrap_node_guid(node)
    _warnings: List[str] = []
    if _alt_used_warning:
        _warnings.append(_alt_used_warning)
    if not _guid:
        _guid, _w0 = _pywrap_recover_node_guid(graph, node, expect_pos=node_pos)
        _warnings.extend(_w0)
        if not _guid:
            _warnings.append(
                "node_id 不可用（裸节点 node_guid 不可达 + 回读未命中）；"
                "可用 added_node 标题 + read_nodes 按标题/坐标反查（R6-4 显式标注）")
        else:
            _warnings.append("node_id 经 list_nodes 回读兜底取得（裸节点 node_guid 不可达）")
    return {
        "added_node": str(node.node_title) if hasattr(node, "node_title") else function_path,
        "node_id": _guid,
        "graph": graph_name,
        "warnings": _warnings,
    }


def _cmd_connect_pins(bp_path: str, connections) -> dict:
    """复用 /connect 的三重验证连线逻辑。"""
    bp = _resolve_blueprint(bp_path)
    if not BRIDGE_AVAILABLE:
        return {"success": False,
                "error": "C++ Bridge 插件未加载"}
    results = []
    for i, conn in enumerate(connections):
        src_node = conn.get("src_node", "")
        src_pin = conn.get("src_pin", "")
        dst_node = conn.get("dst_node", "")
        dst_pin = conn.get("dst_pin", "")
        try:
            ok = _connect_with_triple_check(
                bp, src_node, src_pin, dst_node, dst_pin
            )
            # T-20260805 Bug2: 必须返回 status/reason（blueprint_editor.connect_pins_by_id 依赖）
            results.append({
                "index": i, "connection": conn, "success": ok,
                "status": "connected" if ok else "disconnected",
                "reason": "四重验证通过" if ok else "连线验证失败（见 error）",
            })
        except BridgeError as e:
            results.append({
                "index": i, "connection": conn, "success": False,
                "status": "disconnected",
                "reason": str(e),
                "error": str(e), "pitfall_id": e.pitfall_id
            })
    try:
        bridge.compile_blueprint(bp)
    except Exception as _e:
        _OP_LOG.warning(f"_cmd_connect_pins 编译失败（连线可能已生效但未编译）: {_e}")
    return {"success": all(r["success"] for r in results),
            "results": results}


def _load_error_patterns() -> List[dict]:
    """加载 error_patterns.json 模式库（读不到 → 空列表，不阻断主流程）。

    T-20261024-UE5BRIDGE-COMPILE-DIAGNOSE：从 _cmd_compile_blueprint 抽出。
    路径解析与原逻辑一致：scripts/../config/error_patterns.json，
    UE 内 __file__ 不可靠时兜底 COPAW_WORKSPACE。
    """
    _ep_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'config', 'error_patterns.json'
    )
    # 兜底：UE 内 __file__ 可能不可靠 → 用 COPAW_WORKSPACE
    if not os.path.exists(_ep_path):
        _ws = os.environ.get("COPAW_WORKSPACE",
            os.path.join(os.path.expanduser("~"), ".copaw", "workspaces", "default"))
        _ep_path = os.path.join(_ws, "skills", "ue5-automation", "config", "error_patterns.json")
    if not os.path.exists(_ep_path):
        return []
    try:
        with open(_ep_path, 'r', encoding='utf-8') as epf:
            return list(json.load(epf).get('patterns', []))
    except Exception:
        return []  # 模式库损坏 → 降级为无规则（绝不因此报假错误）


def _compile_bp_and_scan_log(bp) -> dict:
    """编译蓝图 + 读日志尾部 + error_patterns 匹配（公共主体）。

    T-20261024-UE5BRIDGE-COMPILE-DIAGNOSE：把 _cmd_compile_blueprint 的
    「编译 + 日志尾部 200 行扫描」抽成公共函数 —— compile_blueprint 与
    compile_and_diagnose 共用同一份编译/扫描实现，避免两份拷贝在后续维护中
    漂移（双份拷贝曾导致 save/delete 命令的验证口径不一致，见 CHANGELOG）。

    返回:
        {
          "compile_ok": bool,        # 编译动作返回值
          "compile_error": str|None, # 编译调用本身抛异常时的文本
          "tail_lines": list[str],   # 日志尾部原始行（结构化抽取的输入）
          "log_matches": list[dict], # error_patterns 命中记录
          "log_read_error": str|None,# 日志读不到时的原因（诊断透明度；不影响编译结论）
        }
    """
    compile_ok = False
    compile_error = None
    structured = None
    try:
        # T-20261027-UE5BRIDGE-COMPILE-STRUCT：优先结构化编译（一次编译拿
        # FCompilerResultsLog 的节点 token）。旧 DLL 无此入口 → None → 回退日志路径。
        if (BRIDGE_AVAILABLE and bridge is not None
                and hasattr(bridge, "compile_blueprint_structured")):
            try:
                raw = bridge.compile_blueprint_structured(bp)
                if isinstance(raw, str) and raw:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict) and parsed.get("ok"):
                        structured = parsed
                        compile_ok = bool(parsed.get("compiled"))
            except Exception as e:
                # 结构化通道异常 → 不让整个诊断失败：记一条告警，回退旧路径
                _OP_LOG.warning(f"compile_blueprint_structured failed, fallback: {e}")
                structured = None
        if structured is None:
            if BRIDGE_AVAILABLE:
                compile_ok = _safe_call(bridge.compile_blueprint, bp, error_cat="compile")
            else:
                compile_ok = _safe_call(
                    unreal.BlueprintEditorLibrary.compile_blueprint, bp,
                    error_cat="compile"
                )
    except Exception as e:
        compile_error = str(e)

    log_matches = []
    tail_lines = []
    log_read_error = None
    try:
        log_dir = unreal.Paths.project_log_dir()
        # 取最新的 .log 文件
        log_files = sorted(
            [f for f in os.listdir(log_dir) if f.endswith('.log')],
            key=lambda f: os.path.getmtime(os.path.join(log_dir, f)),
            reverse=True
        )
        if log_files:
            log_path = os.path.join(log_dir, log_files[0])
            with open(log_path, 'r', encoding='utf-8', errors='replace') as lf:
                # 读取最后 200 行（覆盖编译日志窗口）
                lines = lf.readlines()
                tail_lines = lines[-200:] if len(lines) > 200 else lines
        else:
            # 为什么也记为读取失败：errors 为空时调用方必须能区分
            # 「日志里没有报错行」与「根本没读到日志」（fail-loud：不假成功）。
            log_read_error = f"日志目录中无 .log 文件: {log_dir}"

        # 加载 error_patterns 匹配（⚠️ 独立于 if/else：无论是否读到日志文件都执行，
        # 但扫描对象是 tail_lines —— 无日志时为空列表，循环自然空转）
        for ptn in _load_error_patterns():
            regex = ptn.get('pattern', {}).get('log_regex', '')
            if not regex or regex == '.*':
                continue  # 跳过通配符（无法精确匹配）
            try:
                _re = re.compile(regex, re.IGNORECASE)
                for line in tail_lines:
                    m = _re.search(line)
                    if m:
                        log_matches.append({
                            'pattern_id': ptn['id'],
                            'title': ptn['title'],
                            'severity': ptn['severity'],
                            'matched_line': line.strip()[:200],
                            'fix_action': ptn.get('fix_action', ''),
                        })
            except Exception as e:
                log_read_error = f"规则匹配异常: {e}"
    except Exception as e:
        # 外层兜底：日志读取的任何意外都不影响编译结论的返回
        log_read_error = f"日志读取异常: {e}"

    return {
        "compile_ok": compile_ok,
        "compile_error": compile_error,
        "tail_lines": tail_lines,
        "log_matches": log_matches,
        "log_read_error": log_read_error,
        # 结构化编译结果（None = 旧 DLL/异常 → 调用方走日志正则）
        "structured": structured,
    }


def _cmd_compile_blueprint(bp_path: str) -> dict:
    """编译蓝图（v0.6 P1：编译后扫描日志匹配错误模式）。

    T-20261024-UE5BRIDGE-COMPILE-DIAGNOSE：编译+日志扫描主体抽至
    _compile_bp_and_scan_log（与 compile_and_diagnose 共用）；本函数返回体
    键集合与语义保持完全不变（success/compiled/compile_status/error/log_matches）。
    """
    bp = _resolve_blueprint(bp_path)
    core = _compile_bp_and_scan_log(bp)
    log_matches = core["log_matches"]

    # ⚠️ `success` 键必须存在：MCP 层判据是 `result.get("success") is False`
    #   （见 `_mcp_handle_one`）——只报 `compiled:false` 会让编译失败以
    #   `isError:false` 返回客户端，agent 据 isError 判定时会当成成功。
    result = {"success": core["compile_ok"], "compiled": core["compile_ok"]}
    # D-2（T-20260913-UE5BRIDGE-DELETE-COMPILE-FIX）：编译后**强制回读**
    result["compile_status"] = _bp_status_str(
        bp, compiled=core["compile_ok"])
    if core["compile_error"]:
        result["error"] = core["compile_error"]
    if log_matches:
        result["log_matches"] = log_matches
    return result


# ══════════════════════════════════════════════════════════
#  T-20261024-UE5BRIDGE-COMPILE-DIAGNOSE · 编译报错结构化诊断
#  ── 设计原则（规格书 P0-编译报错自动修复闭环设计）──
#  · 工具只提供「结构化的错误 + 可定位的锚点」，修复决策交给模型；
#    不搞启发式自动修（启发式会制造假成功，与 fail-loud 纪律冲突）。
#  · 解析不出就降级 null，绝不编造（asset/function/node_guid 均如此）。
#  · UEBox 对照（UAL_CompileAndReport）的节点定位双通道中，「FTokenizedMessage
#    Object Token」需 C++ 侧配合（本版零 C++ 改动，留作后续增强）；本实现先落
#    「正则兜底」通道 —— 与 UEBox 的 message format 同源，覆盖中文/英文引擎。
# ══════════════════════════════════════════════════════════

#: 蓝图编译器报错行（两种已知格式，方括号内 = 「资产 函数 [节点标记] 节点名」上下文）：
#:   中文引擎： LogBlueprintUserMessages: [BP_Pickup OnOverlap 节点 Branch] ...报错文本
#:   英文引擎： LogBlueprintUserMessages: [/Game/Dir/BP_Pickup.BP_Pickup OnOverlap Node Branch] ...
#: ctx 部分按「非贪婪到首个 ]」截取 —— 报错文本自身也可能含 ]，属于正文不参与定位。
_BP_ERR_LINE_RE = re.compile(
    r"LogBlueprintUserMessages:\s*\[(?P<ctx>[^\]]*)\]\s*(?P<msg>.*)$"
)
# 5.1/5.8 编译错误行变体（T-20261025-VERIFY：真机双引擎实锤）——
#   老正则认的 LogBlueprintUserMessages: [ctx] msg 实测未出现；两版真实格式：
#   5.8: LogBlueprint: Error: [AssetLog]<路径>：<正文>（无空格·全角冒号·[编译器]）
#   5.1: LogBlueprint: Error: [AssetLog] <路径> (no disk path found): <正文>
#        （有空格·夹 parenthetical·半角冒号·[Compiler]）
# 通用化：[AssetLog] 后容空格；资产 token 后容 "(...)" 附加段；冒号中英都认；
# ctx 括号（[Compiler]/[编译器]）可选；消息体随引擎本地化原样保留。
_BP_ERR_LINE_RE_V58 = re.compile(
    r"LogBlueprint:\s*Error:\s*\[AssetLog\]\s*(?P<asset>\S+?)"
    r"(?:\s+\([^)]*\))?\s*[:：]\s*"
    r"(?:\[(?P<ctx>[^\]]*)\]\s*)?(?P<msg>.*)$"
)
#: UE 日志行首时间戳（UTC）：[2026.10.25-03.33.28:251]
#: ⚠️ 时间部分也是点号分隔（-HH.MM.SS:ms），不是冒号——写冒号恒不匹配
#: （真机实测：过滤器静默失效，旧行全量放行）。
_LOG_TS_RE = re.compile(
    r"^\[(\d{4})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})"
)
#: 节点标记（中/英引擎各一；整 token 匹配，避免把报错文本里的 "Node" 误当标记）
_BP_ERR_NODE_MARKERS = ("节点", "Node")
#: 引脚提示（规格书 pin_hint：能定位到 pin 就给）。只认引号包裹的形态 ——
#: 无引号的 "Pin Condition" 在真实报错文本里歧义过大（可能是普通单词），宁缺毋滥。
_BP_ERR_PIN_RE = re.compile(
    r"(?:\bPin\b|引脚)\s+'(?P<pin>[^']{1,64})'"
)


def _normalize_bp_err_asset(token: str) -> str:
    """资产 token 归一化。

    · 带路径（英文引擎）：`/Game/Dir/BP_Pickup.BP_Pickup`（无论是否带 `_C` 类
      后缀）→ `/Game/Dir/BP_Pickup`（`.` 分割取路径段 —— 路径本身是权威信息，
      类名段与资产段同名，删除是确定性归一而非猜测）。
    · 短名（中文引擎）：**原样保留**，不做任何变换。
      ⚠️ 为什么不给短名剥 `_C` 后缀：资产名本身可能就叫 `X_C`（真实存在的
      命名风格），剥了会把 `BP_C` 错归成 `BP` —— 为了覆盖一个不出现的格式
      （任务给定的两种格式中短名从不带 `_C`）而引入系统性误判，不划算。
      也不拼造 `/Game/` 前缀 —— 真实资产可能在任何目录，编造路径会把 agent
      引向不存在的资产。
    """
    token = token.strip()
    if not token:
        return None
    if token.startswith("/"):
        return token.split(".", 1)[0]
    return token


def _parse_bp_error_context(ctx: str) -> Dict[str, Optional[str]]:
    """解析方括号内的「资产 函数 [节点标记] 节点名」上下文 → 三元组。

    容错规则（解析不出 → None，绝不编造）：
      · token[0] 恒为资产（UE 编译器格式固定：带路径或短名）；
      · `节点` / `Node` 标记存在 → 标记后所有 token 拼接为节点名（节点标题可含
        空格），标记前的 tokens[1]（若存在）为函数名；
      · 标记缺省 → tokens[1]（若存在）只能确定到「函数/图上下文」，节点名 = None；
        剩余 token 无法归类（可能是函数参数上下文），**不猜** —— 信息保留在
        message 原文里，不丢。
      · 状态机子图场景：标记前可能多于一个上下文 token（子图路径），此时
        function 取 tokens[1]（资产后第一个上下文名）—— 字段语义注明为
        「函数/图上下文」；子图完整路径待 C++ Object Token 通道（后续增强）。
    """
    out: Dict[str, Optional[str]] = {"asset": None, "function": None, "node_name": None}
    tokens = (ctx or "").split()
    if not tokens:
        return out
    out["asset"] = _normalize_bp_err_asset(tokens[0])
    marker_idx = None
    for i in range(1, len(tokens)):
        if tokens[i] in _BP_ERR_NODE_MARKERS:
            marker_idx = i
            break
    if marker_idx is not None:
        out["function"] = tokens[1] if marker_idx > 1 else None
        node_part = " ".join(tokens[marker_idx + 1:]).strip()
        out["node_name"] = node_part or None
    else:
        out["function"] = tokens[1] if len(tokens) > 1 else None
        out["node_name"] = None
    return out


def _match_pattern_for_line(line: str, patterns: List[dict]) -> Optional[dict]:
    """单条报错行 → 首个命中的 error_patterns 模式（与 log_matches 同口径：
    匹配**整行**含 LogBlueprintUserMessages 前缀，因为模式库的 log_regex
    是按完整日志行写的）。未命中 → None。"""
    for ptn in patterns:
        regex = (ptn.get('pattern') or {}).get('log_regex', '')
        if not regex or regex == '.*':
            continue
        try:
            if re.compile(regex, re.IGNORECASE).search(line):
                return ptn
        except re.error:
            continue
    return None


def _bp_title_to_guid_map(bp):
    """节点标题 → NodeGuid 映射（复用既有读取接口 _read_l2_nodes）。

    为什么复用 _read_l2_nodes 而非新开读取通道：
      · read_nodes 命令的 guid 字段已做过 UE5.1「BPNodeInfo.node_guid 直接
        str() 会得到 <Struct 'Guid' ...>」的修复（_pywrap_node_guid 归一化为
        32 位 hex）→ 与 get_node_by_guid 直通，正是增量编辑工具的定位键。
      · GUID 是修复闭环的铰链（规格书：node_guid 定位 → 增量编辑工具修）。

    歧义纪律（PTN-018 教训）：同名节点标题仅按 title 定位不可靠 —— 同一标题
    在映射中出现多个不同 GUID 时，该标题**整条剔除**（node_guid=None + warning），
    绝不随机取其一。

    返回 (mapping|None, warnings)；读取通道不可用 → (None, warnings)。
    """
    if not BRIDGE_AVAILABLE:
        return None, ["C++ Bridge 未加载，节点 GUID 通道不可用（node_guid 全部 null）"]
    try:
        l2 = _read_l2_nodes(bp, include_connections=False)
    except Exception as e:
        return None, [f"节点读取失败，node_guid 映射不可用: {e}"]
    mapping: Dict[str, str] = {}
    ambiguous = set()
    for g in l2.get("graphs", []):
        for n in g.get("nodes", []):
            title = n.get("title")
            guid = n.get("guid")
            if not title or not guid:
                continue
            if title in mapping and mapping[title] != guid:
                ambiguous.add(title)
            elif title not in mapping:
                mapping[title] = guid
    for t in ambiguous:
        mapping.pop(t, None)
    warnings = [f"同名节点标题歧义，node_guid 降级 null: {t}" for t in sorted(ambiguous)]
    return mapping, warnings


def _structured_bp_errors(structured: dict, bp=None):
    """FCompilerResultsLog 结构化消息 → 与日志路径同形状的 errors/warnings。

    T-20261027-UE5BRIDGE-COMPILE-STRUCT：字段保持与 `_extract_blueprint_errors`
    一致（code/message/asset/function/node_guid/node_name/pitfall_ref/fix_hint），
    额外带 `graph` / `nodes[]` / `source=compiler_results_log`。

    优势：node_guid 来自 UEdGraphNode token（权威），不再靠「标题反查 GUID」；
    同一次编译的结果不含历史窗口 → 调用方可以把 may_be_stale 恒置 False。
    """
    patterns = _load_error_patterns()
    errors: List[dict] = []
    warnings_out: List[str] = []
    seen = set()

    def _convert(item: dict, sev: str) -> Dict[str, Any]:
        msg = str(item.get("message") or "").strip()
        if not msg:
            return {}
        guid = str(item.get("node_guid") or "").strip() or None
        key = (msg, guid)
        if key in seen:
            return {}
        seen.add(key)
        entry: Dict[str, Any] = {
            "code": "CompileError.Unclassified",
            "message": msg,
            "asset": str(item.get("asset") or "") or None,
            "function": str(item.get("graph") or ""),
            "node_guid": guid,
            "node_name": str(item.get("node_name") or ""),
            "graph": str(item.get("graph") or ""),
            "severity": sev,
            "pitfall_ref": None,
            "fix_hint": None,
            "source": "compiler_results_log",
        }
        nodes = item.get("nodes") or []
        if isinstance(nodes, list) and nodes:
            entry["nodes"] = [
                {"node_guid": n.get("node_guid"), "node_name": n.get("node_name"),
                 "graph": n.get("graph"), "asset": n.get("asset")}
                for n in nodes if isinstance(n, dict)
            ]
        pin_m = _BP_ERR_PIN_RE.search(msg)
        if pin_m:
            entry["pin_hint"] = pin_m.group("pin")
        ptn = _match_pattern_for_line(msg, patterns)
        if ptn:
            entry["code"] = ptn.get("error_code") or entry["code"]
            entry["pitfall_ref"] = ptn.get("id")
            entry["fix_hint"] = ptn.get("fix_action") or None
        return entry

    for item in (structured.get("errors") or []):
        if isinstance(item, dict):
            entry = _convert(item, "error")
            if entry:
                errors.append(entry)
    for item in (structured.get("warnings") or []):
        if isinstance(item, dict):
            entry = _convert(item, "warning")
            if entry:
                # 警告不是错误：单独放在 errors 里会让修复循环误判 —— 只进
                # warnings 文本（保留消息与定位，agent 可选择性处理）
                loc = (entry.get("node_name") or entry.get("graph")
                       or entry.get("asset") or "")
                warnings_out.append(
                    ("[警告] " + entry["message"] + (f"（{loc}）" if loc else "")))
    return errors, warnings_out


def _extract_blueprint_errors(tail_lines: List[str], bp=None):
    """从日志尾部抽取结构化蓝图编译错误。

    返回 (errors, warnings)：
      errors: [{code, message, asset, function, node_guid, node_name,
                pin_hint?, pitfall_ref, fix_hint}]
      warnings: 诊断过程的非阻断告警（GUID 通道不可用 / 同名歧义 / 日志问题）

    规则映射：每条错误按**整行**匹配 error_patterns（与 log_matches 同口径）
    → pitfall_ref（pattern id）+ fix_hint（pattern 的 fix_action）。
    ⚠️ fix_hint 仅供参考，不代替模型判断 —— 规则库是「已知坑」的提示，
    报错的真实根因可能不在规则库覆盖范围内。
    """
    warnings: List[str] = []
    errors: List[dict] = []
    if not tail_lines:
        return errors, warnings

    patterns = _load_error_patterns()
    guid_map = None
    if bp is not None:
        guid_map, guid_warnings = _bp_title_to_guid_map(bp)
        warnings.extend(guid_warnings)

    seen = set()  # 同一行去重（重复编译会把同一报错留在日志尾部多份）
    for raw in tail_lines:
        line = raw.rstrip("\n")
        m = _BP_ERR_LINE_RE.search(line)
        v58_asset = None
        if m:
            ctx, msg = m.group("ctx"), m.group("msg").strip()
        else:
            # 5.8 变体（T-20261025-VERIFY）：LogBlueprint: Error: [AssetLog]路径：正文
            m2 = _BP_ERR_LINE_RE_V58.search(line)
            if not m2:
                continue
            v58_asset = _normalize_bp_err_asset(m2.group("asset"))
            ctx, msg = m2.group("ctx") or "", m2.group("msg").strip()
        if not msg:
            continue  # 仅有上下文无正文 → 无法给 agent 任何信息，跳过
        key = line.strip()
        if key in seen:
            continue
        seen.add(key)

        info = _parse_bp_error_context(ctx)
        if v58_asset:
            # 5.8 行的资产取自 [AssetLog]（ctx 可能是「编译器」这类本地化标记，
            # 不是资产 token —— 覆写才算数）
            info["asset"] = v58_asset
        entry: Dict[str, Any] = {
            # 归一化错误码：命中规则库 → pattern 的 error_code；
            # 未命中 → 哨兵值（明确「未分类」，不是编造的错误类别）
            "code": "CompileError.Unclassified",
            "message": msg,
            "asset": info["asset"],
            "function": info["function"],
            "node_guid": None,
            "node_name": info["node_name"],
            "pitfall_ref": None,
            "fix_hint": None,
        }
        pin_m = _BP_ERR_PIN_RE.search(msg)
        if pin_m:
            entry["pin_hint"] = pin_m.group("pin")

        # 节点名 → GUID（找不到 = null，绝不编造）
        if guid_map and info["node_name"]:
            guid = guid_map.get(info["node_name"])
            if guid:
                entry["node_guid"] = guid

        ptn = _match_pattern_for_line(line, patterns)
        if ptn:
            entry["code"] = ptn.get("error_code") or entry["code"]
            entry["pitfall_ref"] = ptn.get("id")
            entry["fix_hint"] = ptn.get("fix_action") or None
        errors.append(entry)
    return errors, warnings


def _cmd_compile_and_diagnose(bp_path: str) -> dict:
    """编译蓝图 + 结构化错误诊断（P0 编译报错自动修复闭环 · 诊断侧入口）。

    与 compile_blueprint 的差异：编译结论之外，追加从日志尾部抽取的
    结构化错误列表（asset/function/node_name/node_guid 定位三元组 +
    规则库映射），供修复循环按 GUID 定位 → 增量编辑工具修。

    返回体:
        {
          success / compiled / compile_status —— 与 compile_blueprint 同语义
            （success=编译成功；⚠️ MCP 层 isError 判据依赖 success 键存在），
          errors: [结构化错误]（即使编译失败也尽力给全；日志读不到 → 空列表+告警），
          warnings: [诊断过程告警]，
          fix_hint_note: "fix_hint 仅供参考，不代替模型判断"（固定说明），
          log_matches: [error_patterns 命中]（有命中时才出现）,
          error: 编译调用本身异常文本（有异常时才出现）
        }
    """
    bp = _resolve_blueprint(bp_path)
    core = _compile_bp_and_scan_log(bp)

    structured = core.get("structured")
    if structured:
        # T-20261027-UE5BRIDGE-COMPILE-STRUCT：结构化路径——节点 GUID 来自
        # token（权威），且结果只属于本次编译（无历史窗口 → may_be_stale=False）
        errors, diag_warnings = _structured_bp_errors(structured, bp)
        for _e in errors:
            _e["may_be_stale"] = False
    else:
        errors, diag_warnings = _extract_blueprint_errors(core["tail_lines"], bp)
        # T-20261025-UX：每条错误标注是否可能来自历史窗口（编译成功时 tail 里
        # 仍可能留着上一轮的行）——消除「修复后 errors 非空=没修好」的误读
        for _e in errors:
            _e["may_be_stale"] = bool(core.get("compile_ok"))
    # 语义契约（测试锁定）：errors = 本次编译的错误（与编译结论独立）。
    # 收敛判断由调用方看 compiled/compile_status，不靠 errors 清空。
    # ⚠️ 日志路径注意（T-20261025-VERIFY）：tail 窗口含历史错误行 —— 修复循环
    # 第 N 轮的 errors 可能包含上一轮的行，收敛以 compiled 为准。
    if (not structured and not core.get("compile_ok")
            and not errors and not core.get("log_read_error")):
        # T-20261025-VERIFY-FIX：UE 日志异步落盘有延迟 —— apply_graph 编译失败的
        # 错误行尚未刷盘时紧跟 diagnose（agent 连续调用是毫秒级间隔），tail 窗口
        # 扑空 → compiled=False 但 errors=[] 的自相矛盾返回（真机实测踩中）。
        # 编译失败却一条错误都没提取到 = 明确重读一次（2s 让缓冲落盘），仍空则
        # 如实返回（fail-loud：绝不因等不到就编造）。
        time.sleep(2.0)
        try:
            _log_dir = unreal.Paths.project_log_dir()
            _files = sorted(
                [f for f in os.listdir(_log_dir) if f.endswith('.log')],
                key=lambda f: os.path.getmtime(os.path.join(_log_dir, f)),
                reverse=True)
            if _files:
                with open(os.path.join(_log_dir, _files[0]), 'r',
                          encoding='utf-8', errors='replace') as _lf:
                    _lines = _lf.readlines()
                _tail2 = _lines[-200:] if len(_lines) > 200 else _lines
                errors, diag_warnings = _extract_blueprint_errors(_tail2, bp)
        except Exception:
            pass  # 重读失败 → 保持首次结果
    if core["log_read_error"]:
        # 为什么放 warnings 而不是 error：日志读不到不影响编译结论的确定性，
        # 但必须让调用方知道「errors 为空 ≠ 没有错误」（fail-loud 纪律）。
        diag_warnings.append(f"编译日志不可读，结构化错误列表可能不完整: {core['log_read_error']}")

    result: Dict[str, Any] = {
        # 判读辅助：errors 的作用域 + 唯一收敛信号
        "errors_scope": ("compiler_results_log" if structured
                         else "log_tail_window"),
        "convergence_signal": "compiled",
        "structured": bool(structured),
        "compile_ok": bool(core.get("compile_ok")),
        # ⚠️ success 键必须存在（MCP 层 `result.get("success") is False` 判据，
        #    见 _mcp_handle_one 注释）—— 语义与 compile_blueprint 完全一致。
        "success": core["compile_ok"],
        "compiled": core["compile_ok"],
        "compile_status": (str(structured.get("status") or "")
                           if structured else "") or _bp_status_str(
                               bp, compiled=core["compile_ok"]),
        "errors": errors,
        "warnings": diag_warnings,
        "fix_hint_note": "fix_hint 仅供参考，不代替模型判断",
    }
    if structured:
        result["num_errors"] = int(structured.get("num_errors") or 0)
        result["num_warnings"] = int(structured.get("num_warnings") or 0)
    if core["compile_error"]:
        result["error"] = core["compile_error"]
    if core["log_matches"]:
        result["log_matches"] = core["log_matches"]
    return result


def _cmd_set_pin_default_value(bp_path: str, node_id: str, pin_name: str, new_value: str) -> dict:
    """设置节点引脚默认值（白名单命令）。

    T-20260805-UE-FULL-CLOSURE · P0-1 新增：
      - 严格参数白名单校验：bp_path / node_id / pin_name / new_value 必须全部为 str
      - new_value 长度限制 + 禁换行（防注入攻击）
      - 调用签名与真实 C++ DLL 对齐：SetPinDefaultValue(Node, PinName, Value) —— 3 参
      - 禁任意代码执行路径（无 exec_code 回退；不走 /execute）
      - 走 GameThread 原子执行（与 /build /connect 一致）

    Returns:
        {"ok": True, "node": node_id, "pin": pin_name, "value": new_value}
    """
    # ── 类型 + 安全检查 ──
    if not isinstance(bp_path, str) or not isinstance(node_id, str) \
       or not isinstance(pin_name, str) or not isinstance(new_value, str):
        raise BridgeError(
            "参数类型错误：bp_path/node_id/pin_name/new_value 必须全部为字符串",
            category="param_type",
        )
    if not node_id.strip():
        raise BridgeError(
            "node_id 为空：必须给节点标题（如 打印字符串 / PrintString）"
            "或 32 位 GUID（推荐，同名节点唯一可靠锚点）",
            category="param_type",
        )
    if not pin_name.strip():
        raise BridgeError(
            "pin_name 为空：必须给引脚名（如 InString / then）",
            category="param_type",
        )
    if len(new_value) > 500:
        raise BridgeError(
            f"new_value 超长（{len(new_value)}>500 字符），拒绝执行",
            category="param_type",
        )
    if "\n" in new_value or "\r" in new_value:
        raise BridgeError("new_value 禁止包含换行符", category="param_type")

    # ── 解析蓝图 + 确认 pin + 设置默认值（T-20260805 修复：GameThread 原子执行；set 需 K2Node，8/20 在线实测确认）──
    def _do():
        BPB = unreal.BlueprintPythonBridge
        bp = unreal.EditorAssetLibrary.load_asset(bp_path)
        if not bp:
            raise BridgeError(f"蓝图加载失败: {bp_path}", category="load_failure")
        graph = BPB.get_event_graph(bp)
        if not graph:
            raise BridgeError("EventGraph 不存在", category="graph_failure")

        # ── D-2（T-20260916-UE5AUTO-HALLBEACON-D1D4-FIX）：node_id 支持 GUID 语义 ──
        #   旧实现只有标题定位 → 3× 同名「设置Actor在游戏中隐藏」无法分别设值
        #   （P5 实测：3 个隐藏位需 true/false/true，只能靠 /build 的
        #   functions[].defaults 在进程内按**对象引用**绕开）。
        #   修法：node_id 形如 GUID（`_pywrap_is_guid_like`）→ 复用既有
        #   `_pywrap_find_graph_by_guid`（C++ GetNodeByGuid 通路）+ 回读表取
        #   BPNodeInfo —— **全部 Python 侧 pywrap，零 DLL 改动**；
        #   标题定位保持向后兼容（唯一标题时行为完全不变）。
        _guid_mode = _pywrap_is_guid_like(node_id)
        k2 = None
        _pin_graph = graph
        if _guid_mode:
            _g, k2, _gerr = _pywrap_find_graph_by_guid(bp, node_id)
            if k2 is None:
                raise BridgeError(
                    f"节点 GUID 未命中: {node_id}（{_gerr}）→ GUID 定位要求 node_id "
                    "为 32 位十六进制串，且节点存在于该蓝图任一图中",
                    category="node_failure",
                )
            _pin_graph = _g or graph
            node_info = _pywrap_node_info_by_guid(_pin_graph, node_id)
            if node_info is None:
                raise BridgeError(
                    f"节点 GUID 已定位到 K2Node，但 BPNodeInfo 回读为空: {node_id}"
                    "（回读通道可能不可用；pin 校验需 BPNodeInfo）",
                    category="node_failure",
                )
        else:
            node_info = _find_bp_node_info(graph, node_id)
            if not node_info:
                raise BridgeError(f"节点未找到: {node_id}", category="node_failure")

        # T-20260805 Bug1（正确版）: pin 检查用 get_pin_infos(BPNodeInfo)——C++ 签名
        #   GetPinInfos(UEdGraph*, const FBPNodeInfo&)（真实 UE 实测可返回 InString）。
        #   ⚠️ 不能用 K2Node.pins：真实 UE 5.1 Python 绑定无该属性（AttributeError）。
        pin_ok = False
        try:
            for p in (BPB.get_pin_infos(_pin_graph, node_info) or []):
                if str(getattr(p, "pin_name", "")) == pin_name:
                    pin_ok = True
                    break
        except Exception as pe:
            raise BridgeError(f"引脚检查异常: {node_id}.{pin_name}: {pe}", category="pin_failure")
        if not pin_ok:
            raise BridgeError(f"引脚不存在: {node_id}.{pin_name}", category="pin_failure")

        # T-20260820-UE5BRIDGE-SET-FIX（在线对比落定）：SetPinDefaultValue 签名
        #   SetPinDefaultValue(UEdGraphNode*, FString, FString) → 必须传 K2Node
        #   （find_node_by_title 返回）。8/20 UE 在线 10 次对比实测：
        #   K2Node 10/10 成功；BPNodeInfo 0/10 TypeError（Failed to convert
        #   parameter 'node'）→ 确认传 K2Node，不可传 BPNodeInfo。
        #   k2 查找走 _find_node_by_title_i18n（与 connect 命令对齐）：node_id
        #   为英文而 UE 标题被 i18n 本地化时，裸 find_node_by_title 找不到。
        if k2 is None:
            k2 = _find_node_by_title_i18n(graph, node_id)
        if not k2:
            raise BridgeError(f"节点未找到(原生): {node_id}", category="node_failure")
        ok = BPB.set_pin_default_value(k2, pin_name, new_value)
        if not ok:
            raise BridgeError(
                f"SetPinDefaultValue 返回 False: {node_id}.{pin_name}",
                category="pin_failure",
            )
        return {"ok": True, "node": node_id, "pin": pin_name, "value": new_value,
                "matched_by": "guid" if _guid_mode else "title"}

    return _run_on_game_thread_sync(_do, timeout=30.0)


def _cmd_get_pin_default_value(bp_path: str, node_id: str, pin_name: str) -> dict:
    """读取节点引脚当前默认值（白名单命令，供 _verify_param 白名单化）。

    T-20260805-UE-FULL-CLOSURE · P0-1 新增：
      - 与 _cmd_set_pin_default_value 对偶的只读命令
      - 严格类型校验 + GameThread 原子执行
      - 禁任意代码执行路径

    Returns:
        {"node": node_id, "pin": pin_name, "value": str 或 None}
    """
    if not isinstance(bp_path, str) or not isinstance(node_id, str) \
       or not isinstance(pin_name, str):
        raise BridgeError(
            "参数类型错误：bp_path/node_id/pin_name 必须全部为字符串",
            category="param_type",
        )
    if not node_id.strip():
        raise BridgeError(
            "node_id 为空：必须给节点标题（如 打印字符串 / PrintString）"
            "或 32 位 GUID（推荐，同名节点唯一可靠锚点）",
            category="param_type",
        )
    if not pin_name.strip():
        raise BridgeError(
            "pin_name 为空：必须给引脚名（如 InString / then）",
            category="param_type",
        )

    def _do():
        BPB = unreal.BlueprintPythonBridge
        bp = unreal.EditorAssetLibrary.load_asset(bp_path)
        if not bp:
            raise BridgeError(f"蓝图加载失败: {bp_path}", category="load_failure")
        graph = BPB.get_event_graph(bp)
        if not graph:
            raise BridgeError("EventGraph 不存在", category="graph_failure")
        # ── D-2（T-20260916-UE5AUTO-HALLBEACON-D1D4-FIX）：node_id 支持 GUID 语义
        #   —— 与 `_cmd_set_pin_default_value` 对偶（同名节点唯一可靠锚点）。
        _guid_mode = _pywrap_is_guid_like(node_id)
        _pin_graph = graph
        if _guid_mode:
            _g, _k2, _gerr = _pywrap_find_graph_by_guid(bp, node_id)
            if _k2 is None:
                raise BridgeError(
                    f"节点 GUID 未命中: {node_id}（{_gerr}）→ GUID 定位要求 node_id "
                    "为 32 位十六进制串，且节点存在于该蓝图任一图中",
                    category="node_failure",
                )
            _pin_graph = _g or graph
            node_info = _pywrap_node_info_by_guid(_pin_graph, node_id)
            if node_info is None:
                raise BridgeError(
                    f"节点 GUID 已定位到 K2Node，但 BPNodeInfo 回读为空: {node_id}"
                    "（回读通道可能不可用；get 需 BPNodeInfo 读 pin 默认值）",
                    category="node_failure",
                )
        else:
            node_info = _find_bp_node_info(graph, node_id)
            if not node_info:
                raise BridgeError(f"节点未找到: {node_id}", category="node_failure")

        # T-20260805 Bug5（正确版）: 用 GetPinInfos(graph, BPNodeInfo) 拿 pin + default_value
        #   真实 UE 5.1 实测可返回 InString + "Hello from BP!"
        #   ⚠️ 不能用 BPB.list_pins(K2Node)：拿不到正确 pin 列表
        #   ⚠️ 必须在 GameThread 跑（list_nodes/get_pin_infos 都是异步），否则返回空
        value = None
        found = False
        try:
            for p in (BPB.get_pin_infos(_pin_graph, node_info) or []):
                pn = str(getattr(p, "pin_name", ""))
                if pn == pin_name:
                    value = str(getattr(p, "default_value", ""))
                    found = True
                    break
        except Exception:
            found = False
        if not found:
            raise BridgeError(f"引脚不存在: {node_id}.{pin_name}", category="pin_failure")
        return {"node": node_id, "pin": pin_name, "value": value,
                "matched_by": "guid" if _guid_mode else "title"}

    return _run_on_game_thread_sync(_do, timeout=30.0)


# ══════════════════════════════════════════════════════════
#  PATCH-A/B 新接口 · L1 Python 封装（T-20260910-UE5BRIDGE-PYWRAP-L1-IMPL）
# ══════════════════════════════════════════════════════════
# 🔴 KB 语义搜索结果（编码前 · 2026-09-10 · kb=agent_knowledge）：
#   1. 「read_variables 假阳性修复模式」[已验证 2026-08-20]：吞异常 → 返回
#      success=true 的假阳性结构，是**最难排查的失败模式之一**。正确姿势 =
#      数据层抛 BridgeError(category/suggestion)、命令层 success=false + error
#      显式字段。→ 本批 11 个命令**一律 fail-loud**：底层返回 None/False →
#      必抛 BridgeError，绝不返回 "success": true 的假阳性。
#   2. 「T-20260723-THREAD-WRITE / THREAD-READ 复盘」：Bridge C++ 侧 20 个 UObject
#      访问函数已全部 `Async(EAsyncExecution::TaskGraphMainThread, ...)` 包裹 +
#      `IsInGameThread()` 守卫 → uvicorn 线程可直接调 `bridge.*`（既有
#      `_cmd_add_function_node` / `_connect_with_triple_check` 即此模式，生产已验证）。
#      本批沿用同一模式，**不额外套 `_run_on_game_thread_sync`**（KB 明确：
#      GameThread 内调用 bridge.* 包装函数 → AsyncTask 嵌套死锁）。
#   3. 「UE 节点标题本地化」[2026-08-20]：中文编辑器下节点标题被本地化
#      （BeginPlay → 事件开始运行）→ 回读断言**不得**依赖纯英文标题，一律用
#      `in` 包含判断；跨调用锚点一律用 **GUID 字符串**（对象引用跨 exec 可能失活）。
#
# 契约（对齐 PYWRAP-L1 评估单 §3）：
#   A. `bool + FString&` → None=失败 / ''=成功 / 非空文本=失败(错误文本)
#   B. 指针返回 → None = 失败 → raise（禁止静默返回 None）
#   C. 统一返回 {"ok": True, "data"/字段..., "warnings": [...]}；失败 raise
#   D. 跨调用只用 GUID 字符串；warnings 记录「回读不可用」等非致命降级
# ══════════════════════════════════════════════════════════

_PYWRAP_IMPL_ID = "T-20260910-UE5BRIDGE-PYWRAP-L1-IMPL"

# Timeline 节点契约引脚（评估单 L1-8 · T-20260910 S4 在线实测 10 枚）
_TIMELINE_EXPECTED_PINS = ("Play", "PlayFromStart", "Stop", "Reverse",
                           "ReverseFromEnd", "Update", "Finished",
                           "SetNewTime", "NewTime", "Direction")

# bare 类名 → 对象路径候选包（按命中概率排序）
_NODE_CLASS_PACKAGES = (
    "/Script/BlueprintGraph.",
    "/Script/UnrealEd.",
    "/Script/Engine.",
)


def _pywrap_require_bridge():
    """断言 C++ Bridge 可用（fail-loud，杜绝静默 None 假阳性）。"""
    if not BRIDGE_AVAILABLE or bridge is None:
        raise BridgeError(
            "C++ BlueprintPythonBridge 插件未加载，L1 节点命令不可用",
            category="bridge_unavailable",
            suggestion="确认 Plugins/BlueprintPythonBridge 已启用并重启 UE 编辑器",
        )


def _pywrap_require_str(name: str, val, allow_empty: bool = False) -> str:
    """参数类型 + 空值硬校验（fail-loud，category=param_type）。"""
    if not isinstance(val, str):
        raise BridgeError(
            f"参数类型错误：{name} 必须为字符串（收到 {type(val).__name__}）",
            category="param_type",
        )
    if not allow_empty and not val.strip():
        raise BridgeError(f"参数为空：{name} 不可为空字符串", category="param_type")
    return val


def _pywrap_bool(name: str, val) -> bool:
    """布尔参数归一化（JSON bool / 'true' / 'false' 均接受，其余 fail-loud）。"""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        low = val.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    raise BridgeError(
        f"参数类型错误：{name} 必须为布尔（收到 {val!r}）", category="param_type")


def _pywrap_pos(node_pos):
    """`node_pos=[x, y]` → (int, int)；None → (0, 0)；非法 → faid-loud。"""
    if node_pos is None:
        return 0, 0
    try:
        x, y = node_pos[0], node_pos[1]
    except Exception:
        raise BridgeError(
            f"node_pos 格式错误（应为 [x, y]）: {node_pos!r}", category="param_type")
    try:
        return int(x), int(y)
    except Exception:
        raise BridgeError(
            f"node_pos 必须为数值: {node_pos!r}", category="param_type")


def _pywrap_norm_guid(guid) -> str:
    """GUID 归一化 → 「大写 + 去 {} 去 -」的十六进制串（跨调用比较用）。

    兼容三种形态：`FGuid.export_text()`（32 位无分隔）/ `str(FGuid)`（FGuid(...)）/
    带花括号或连字符的 UUID。统一后既可用于比较，也可回传 `get_node_by_guid`。
    """
    s = str(guid or "")
    for ch in ("{", "}", "-", "(", ")", "[", "]", '"', "'", ",", " ", "\t"):
        s = s.replace(ch, "")
    s = s.strip().upper()
    if s.startswith("FGUID"):
        s = s[5:]
    if s.startswith("0X"):
        s = s[2:]
    return s


def _pywrap_is_guid_like(value) -> bool:
    """入参形态是否「形如 GUID」→ 归一化后恰为 32 位十六进制串。

    T-20260911-UE5BRIDGE-DELNODE-GUID：`/delete_node` 的 `node_id` 兼容两种形态
    （32 位 hex GUID / 节点标题）。判定用 `_pywrap_norm_guid` 剥离 `{}`/`-`/空白
    等分隔符后做长度 + 字符集校验，故 `{XXXXXXXX-....}` / 裸 32 位 hex 均命中。
    标题（如 `打印字符串` / `PrintString`）归一化后不满足 32 位 hex → 走标题路径。
    """
    s = str(value or "").strip()
    if not s:
        return False
    norm = _pywrap_norm_guid(s)
    if len(norm) != 32:
        return False
    return all(c in "0123456789ABCDEF" for c in norm)


def _pywrap_node_guid(node) -> str:
    """取节点 GUID 字符串（优先 export_text，其次 str()）。失败返回 ''。"""
    try:
        g = getattr(node, "node_guid", None)
    except Exception:
        return ""
    if g is None:
        return ""
    export = getattr(g, "export_text", None)
    if callable(export):
        try:
            return _pywrap_norm_guid(export())
        except Exception:
            pass
    return _pywrap_norm_guid(g)


def _pywrap_node_title(node) -> str:
    """取节点标题（容错，失败返回 ''）。"""
    try:
        return str(getattr(node, "node_title", "") or "")
    except Exception:
        return ""


def _pywrap_recover_node_guid(graph, node, expect_pos=None) -> tuple:
    """H-1 **GUID 回读兜底**：裸 `K2Node.node_guid` 不可达时经 `list_nodes()` 取真 GUID。

    R6-4（T-20260917-D5D6-CONTRACT-FIX）：DLL 建节点返回的是裸 `unreal.K2Node`，
    UE 5.1 下其 `NodeGuid` 常不可达（`UPROPERTY()` 缺 BlueprintReadOnly 标记）
    → `_pywrap_node_guid(node)` 恒 `''`。真 GUID 只能经 `list_nodes()` 的
    `BPNodeInfo.node_guid` 取得（同源口径：`_pywrap_snapshot_guids` L1802）。

    定位策略（**确定性优先，歧义绝不猜**）：
      1. 标题**唯一**命中 → 采用
      2. 标题多命中 / 无标题 → 用 `expect_pos` 位置消歧（唯一命中才采用）
      3. 全图仅一个候选 → 采用
      4. 仍歧义 / 无候选 → 返回 `''` + warning（**由调用方显式标注，禁静默空串**）

    返回 `(guid, warnings)`。
    """
    warnings: List[str] = []
    _title = _pywrap_node_title(node)
    infos, err = _pywrap_readback_nodes(graph)
    if err:
        warnings.append("node_id 回读不可用（%s）" % err)
        return "", warnings
    cands = [i for i in (infos or []) if i.guid]
    if not cands:
        warnings.append("node_id 回读为空（list_nodes 无可用 GUID）")
        return "", warnings

    if _title:
        same = [i for i in cands if i.title == _title]
        if len(same) == 1:
            return same[0].guid, warnings
        if same:
            cands = same            # 同名多节点 → 继续用位置消歧
        else:
            warnings.append("node_id 回读未命中标题 %r" % _title)

    if expect_pos is not None:
        try:
            want = (int(expect_pos[0]), int(expect_pos[1]))
        except Exception:
            want = None
        if want is not None:
            hits = [i for i in cands if (i.pos_x, i.pos_y) == want]
            if len(hits) == 1:
                return hits[0].guid, warnings

    if len(cands) == 1:
        return cands[0].guid, warnings
    warnings.append("node_id 回读歧义（%d 个候选，无法唯一定位 → 不猜）"
                    % len(cands))
    return "", warnings


def _pywrap_read_pin_names(node) -> List[str]:
    """读节点引脚名列表（list_pins 优先；异常 → 空列表，不阻断主流程）。"""
    try:
        pins = bridge.list_pins(node)
    except Exception:
        return []
    names = []
    for p in (pins or []):
        n = _pin_name(p)
        if n:
            names.append(n)
    return names


class _PyWrapNodeInfo:
    """节点回读视图：统一真实 UE BPNodeInfo 与 fake 替身的属性差异。

    真实 UE `list_nodes()` 返回 FBPNodeInfo（node_title / node_class /
    node_guid / pos_x / pos_y —— 见 `_read_l2_nodes` 生产实现）；fake_unreal
    的 FakeNode 同名属性（node_guid 为 uuid 字符串）。
    """

    __slots__ = ("raw", "guid", "title", "node_class", "pos_x", "pos_y")

    def __init__(self, raw):
        self.raw = raw
        self.guid = _pywrap_node_guid(raw)
        self.title = _pywrap_node_title(raw)
        try:
            self.node_class = str(getattr(raw, "node_class", "?"))
        except Exception:
            self.node_class = "?"
        try:
            self.pos_x = int(getattr(raw, "pos_x", 0))
        except Exception:
            self.pos_x = 0
        try:
            self.pos_y = int(getattr(raw, "pos_y", 0))
        except Exception:
            self.pos_y = 0


def _pywrap_readback_nodes(graph):
    """回读图的节点清单 → (list[_PyWrapNodeInfo], err_str)。

    R1 回读降级策略（本批统一）：**能读到就严格比对；读不到不阻断主流程**，
    只写 warnings。原因 — `list_nodes` 属读取族，在非 GameThread 上下文可能
    返回空（见 `_cmd_get_pin_default_value` 头注「必须在 GameThread 跑」），
    若把「读不到」等同于「操作失败」会制造假阴性；而 S2 在线联调会用本函数
    的 warnings 判定读取族在 uvicorn 线程下的可用性。
    """
    try:
        infos = bridge.list_nodes(graph)
    except Exception as e:
        return [], f"list_nodes 异常: {e}"
    out = []
    for n in (infos or []):
        out.append(_PyWrapNodeInfo(n))
    return out, ""


def _pywrap_find_info(graph, guid):
    """按 GUID 回读节点信息 → (_PyWrapNodeInfo | None, err_str)。"""
    target = _pywrap_norm_guid(guid)
    if not target:
        return None, "GUID 为空，无法回读"
    infos, err = _pywrap_readback_nodes(graph)
    for info in infos:
        if info.guid == target:
            return info, ""

def _pywrap_node_info_by_guid(graph, guid):
    """GUID → **原始 BPNodeInfo**（`get_pin_infos(graph, info)` 直接可吃）。

    T-20260916-UE5AUTO-HALLBEACON-D1D4-FIX · D-2 新增：`get_pin_infos` 的 pin
    校验吃 **FBPNodeInfo**（USTRUCT），而 GUID 定位原语
    `_pywrap_find_graph_by_guid` 返回的是 **K2Node** → 二者需各取一份
    （K2Node 给 `set_pin_default_value`，BPNodeInfo 给 `get_pin_infos`）。

    ⚠️ 必须取 `_PyWrapNodeInfo.raw`（即 `list_nodes()` 原样返回的结构体）；
    直接把包装对象喂给 `get_pin_infos` → `AttributeError: ... has no attribute
    'pins'`（离线替身实测）→ 被 `except` 转成「引脚检查异常」假阴性。
    （同源口径：`_pywrap_verify_pin_disconnected` L6212 亦走
    「BPNodeInfo（`_PyWrapNodeInfo.raw`）」。）
    """
    target = _pywrap_norm_guid(guid)
    if not target:
        return None
    try:
        infos, _err = _pywrap_readback_nodes(graph)
    except Exception:
        return None
    for info in (infos or []):
        if info.guid == target:
            return info.raw
    return None


def _pywrap_snapshot_guids(graph):
    """建节点操作**前**取节点 GUID 快照 → (set[str] | None, err_str)。

    H-1 根因：DLL 建节点函数返回的裸 `unreal.K2Node` 在 UE 5.1 Python 下
    `NodeGuid` / `NodeTitle` 不可达（`UPROPERTY()` 无 BlueprintReadOnly 标记）
    → 恒返回 ''。真 GUID 只能经 `list_nodes()` 的
    `BPNodeInfo.node_guid.export_text()` 取得。
    `None` = 快照通道不可用（list_nodes 异常）→ 调用方只告警、不阻断。
    """
    infos, err = _pywrap_readback_nodes(graph)
    if err:
        return None, err
    return {i.guid for i in infos if i.guid}, ""


def _pywrap_locate_new_nodes(graph, before_guids):
    """建节点操作**后**做快照差集 → (list[_PyWrapNodeInfo], warnings)。

    差集 = 本次操作新落的节点（确定性高，不依赖标题模糊匹配）。
    """
    warnings = []
    if before_guids is None:
        warnings.append("操作前节点快照不可用 → 无法用快照差集定位新节点 GUID")
        return [], warnings
    infos, err = _pywrap_readback_nodes(graph)
    if err:
        warnings.append(f"操作后节点回读失败: {err}")
        return [], warnings
    new = [i for i in infos if i.guid and i.guid not in before_guids]
    if not new:
        warnings.append("建节点后快照差集为空（未定位到新节点；回读通道可能不可用）")
    elif len(new) > 1:
        warnings.append(
            f"建节点后快照差集非唯一（{len(new)} 个新节点）: "
            f"{[(i.guid, i.title) for i in new]}")
    return new, warnings


def _pywrap_pick_new_node(new_infos, expect_pos=None, label=""):
    """从快照差集中挑出目标节点 → (_PyWrapNodeInfo | None, warnings)。"""
    warnings = []
    if not new_infos:
        return None, warnings
    if len(new_infos) == 1:
        return new_infos[0], warnings
    if expect_pos is not None:
        try:
            want = (int(expect_pos[0]), int(expect_pos[1]))
        except Exception:
            want = None
        if want is not None:
            hits = [i for i in new_infos if (i.pos_x, i.pos_y) == want]
            if len(hits) == 1:
                return hits[0], warnings
    warnings.append(
        f"{label}: 快照差集非唯一且按位置无法消歧（{len(new_infos)} 个候选）")
    return None, warnings


def _pywrap_describe_infos(infos):
    """节点信息 → 可 JSON 化的标识（残留 / 污染汇报用）。"""
    return [{"guid": i.guid, "title": i.title,
             "node_class": i.node_class, "pos": [i.pos_x, i.pos_y]}
            for i in infos]


def _pywrap_iter_real_nodes(graph):
    """枚举图内**真实节点对象**（`UEdGraphNode*`）。

    `bridge.list_nodes()` 返回的是 `FBPNodeInfo`（USTRUCT，**不是节点**）→ 直接喂
    `bridge.list_pins()` 会被 UE 报
    `NativizeProperty: Cannot nativize 'BPNodeInfo' as 'Node'`（**M-2 根因**），
    故用 `get_node_by_guid` 逐条换回真实节点；换不回的跳过（不阻断，避免假阴性）。
    """
    infos, err = _pywrap_readback_nodes(graph)
    if err:
        return []
    out = []
    for info in infos:
        if not info.guid:
            continue
        try:
            n = bridge.get_node_by_guid(graph, info.guid)
        except Exception:
            n = None
        if n is not None:
            out.append(n)
    return out


def _pywrap_delete_node_by_guid(graph, guid):
    """按 **GUID** 精确删除图中的节点 → (ok: bool, detail: str)。

    H-2 残留清理的删除原语（T-20260910-UE5BRIDGE-CLEANUP-GUID）：
        node = bridge.get_node_by_guid(graph, guid)   # GUID → 真实 UEdGraphNode*
        ok   = bridge.remove_node(graph, node)        # C++ RemoveNode

    **为什么必须按 GUID**：残留节点是「本次操作新落、随后失败」的节点；当它的
    标题与图中**既有节点重名**时（复现脚本用既有变量名逼负路径），按标题删除会
    删掉无辜的既有节点 —— N-1 复验实测节点数 31→30（见
    `output/T-20260910-UE5BRIDGE-CAPABILITY-AUDIT/PYWRAP-N1-修复与复验报告-20260910.md` §7）。
    这里的 GUID 来自建节点**前的快照差集**，只可能是本次新落的节点 → 天然不误伤。

    与 H1H2 报告 §3-c 反证用例的回收路径是**同一个删除对**，该路径已在线实测
    `residue_left=[]`。C++ `RemoveNode` 自身清理该节点全部连线（支持已连接节点），
    且 `Graph->Modify()` 会置包为脏 → 本原语**不**手工断线、**不**额外 mark dirty
    （端点路径 `_delete_node_on_game_thread` 按标题定位、需先断线，两者不同源）。

    返回 (False, detail) = **未删除** → 调用方据此判定「图已污染」并 fail-loud。
    """
    if not guid:
        return False, "GUID 为空，无法按 GUID 精确删除"
    if not hasattr(bridge, "remove_node"):
        return False, "C++ RemoveNode 不可用（Bridge 插件未加载）"
    try:
        node = bridge.get_node_by_guid(graph, guid)
    except Exception as e:
        return False, f"get_node_by_guid 异常 {type(e).__name__}: {e}"
    if node is None:
        return False, "get_node_by_guid 未命中（节点对象不可达）"
    try:
        removed = bridge.remove_node(graph, node)
    except Exception as e:
        return False, f"remove_node 异常 {type(e).__name__}: {e}"
    if not removed:
        return False, "C++ RemoveNode 返回 False"
    return True, ""


def _pywrap_cleanup_residue(graph, bp, before_guids):
    """失败分支残留清理（H-2）→ dict（不抛异常，只如实回报）。

    定位：建节点**前**快照差集（`_pywrap_locate_new_nodes` —— 只可能命中本次新落节点）。
    删除：**按 GUID 精确删除**（`_pywrap_delete_node_by_guid` →
    `bridge.get_node_by_guid` + C++ `RemoveNode`）。
      ⚠️ T-20260910-UE5BRIDGE-CLEANUP-GUID 起由「按标题删除」改为「按 GUID 删除」：
         标题可重名 → 按标题删会误删图中既有节点（N-1 复验 §7 实测 31→30）。
    删除后**复读验证**差集是否清空；删不掉 → `verified=False` → 上层 fail-loud。

    `bp` 形参保留（调用方签名稳定；GUID 删除路径不经蓝图对象）。
    """
    out = {"attempted": False, "residue": [], "deleted": [], "deleted_guids": [],
           "remaining": [], "verified": True, "detail": ""}
    if before_guids is None:
        out["verified"] = False
        out["detail"] = ("操作前节点快照不可用 → 无法安全定位残留节点"
                         "（未执行删除）")
        return out
    resid, _w = _pywrap_locate_new_nodes(graph, before_guids)
    if not resid:
        out["detail"] = "快照差集为空 → 无残留节点"
        return out
    out["attempted"] = True
    out["residue"] = _pywrap_describe_infos(resid)
    errs = []
    for info in resid:
        guid = info.guid
        if not guid:
            errs.append("快照差集条目缺 GUID → 无法按 GUID 精确删除"
                        f"（title={info.title or '<空>'}）")
            continue
        ok, why = _pywrap_delete_node_by_guid(graph, guid)
        if ok:
            out["deleted"].append(info.title or guid)
            out["deleted_guids"].append(guid)
        else:
            errs.append(f"guid={guid}（title={info.title or '<空>'}）: "
                        f"删除失败（{why}）")
    after, _e = _pywrap_snapshot_guids(graph)
    if after is None:
        out["verified"] = False
        errs.append("删除后复读不可用 → 无法确认已回落")
    else:
        out["remaining"] = sorted(after - before_guids)
        out["verified"] = ((not out["remaining"])
                           and len(out["deleted"]) == len(resid))
    out["detail"] = "; ".join(errs)
    return out


def _pywrap_fail_with_cleanup(graph, bp, before_guids, label, err):
    """建节点失败统一出口：先清残留（H-2），再 fail-loud 抛出（绝不静默）。

    清理成功 → 原错误 + 清理轨迹；清理失败 → 显式「图已污染 + 残留节点标识」。
    本函数**总是抛异常**（调用点其后代码不可达）。
    """
    cl = _pywrap_cleanup_residue(graph, bp, before_guids)
    msg = str(err)
    cat = getattr(err, "category", "node_add") or "node_add"
    pit = getattr(err, "pitfall_id", "") or ""
    sug = getattr(err, "suggestion", "") or ""
    if cl["attempted"] and not cl["verified"]:
        raise BridgeError(
            f"{msg} | ⚠️ 图已污染：残留节点清理失败 → "
            f"residue={json.dumps(cl['residue'], ensure_ascii=False)} "
            f"remaining={cl['remaining']} detail={cl['detail']}",
            category=cat, pitfall_id=pit,
            suggestion="图已污染（可能致蓝图编译失败）：请人工删除残留节点后再"
                       "编译；本接口不静默放过",
        ) from err
    if cl["attempted"]:
        msg += (f" | 已自动清理残留节点 {cl['deleted']}"
                f"（快照差集反查 + 复读验证 remaining={cl['remaining']}）")
    raise BridgeError(msg, category=cat, pitfall_id=pit, suggestion=sug) from err


def _pywrap_resolve_graph(bp, graph_name: str):
    """按图名解析 UEdGraph（EventGraph 走快路径，其余遍历 get_all_graphs）。

    失败 fail-loud，错误信息附带可用图名清单（可诊断性）。
    """
    gname = graph_name or "EventGraph"
    if gname == "EventGraph":
        try:
            g = bridge.get_event_graph(bp)
        except Exception as e:
            raise BridgeError(f"get_event_graph 异常: {e}", category="graph_failure")
        if g is None:
            raise BridgeError("EventGraph 不存在", category="graph_failure")
        return g
    try:
        graphs = bridge.get_all_graphs(bp) or []
    except Exception as e:
        raise BridgeError(f"get_all_graphs 异常: {e}", category="graph_failure")
    names = []
    for g in graphs:
        try:
            nm = str(g.get_name())
        except Exception:
            continue
        names.append(nm)
        if nm == gname:
            return g
    raise BridgeError(
        f"图表未找到: {gname}（可用: {', '.join(names) or '无'}）",
        category="graph_failure",
    )


# ══════════════════════════════════════════════════════════════
# D-1 加固（T-20260912-UE5BRIDGE-PYWRAP-L2-GUARD）· 引擎类路径前置解析守卫
# ══════════════════════════════════════════════════════════════
# 【事故 · P0 崩溃级】`add_implemented_interface` 把用户字符串**原样以 FName 交给**
#   `FBlueprintEditorUtils::ImplementNewInterface`（C++ 侧
#   BlueprintPythonBridge.cpp:2841-2843 无任何解析/守卫）→ 引擎内部
#   `check(InterfaceClass)`（BlueprintEditorUtils.cpp:6463）断言失败 →
#   **UnrealEditor 硬崩，未存盘进度全丢**。
#   2026-09-12 19:1x 实测（PID 68416）：raw/b5_addiface_bogus.json =
#     {"success": false, "error": "TRANSPORT: ConnectionResetError:
#      [WinError 10054] 远程主机强迫关闭了一个现有的连接。"}
#
# 【同族先例 · 同一 .cpp 内的正确处理】cpp:973-1026（SCS 外链自愈修复）：
#   在**调用引擎前**先做空值判定并**拒绝调用** + 回传显式错误文本
#   （"...engine check() at SimpleConstructionScript.cpp:1375 would hard-crash
#   the editor"）。本守卫把同一原则搬到 Python 侧：**先解析，解析不到不调引擎**。
#
# 【解析口径】C++ 侧经 FindObject/LoadObject 解析；Python 侧用
#   `unreal.load_class` / `unreal.load_object` 试解析——解析成功的副作用 =
#   把类/枚举载入内存，从而让引擎后续的 FindObject(ANY_PACKAGE, ...) 也能命中。
#
# 【分级（递引擎前解析预检 · R3 硬化后）】
#   · Tier A · 硬 fail-loud：入参为**全路径**（以 "/" 开头）且全部候选解析失败
#     → BridgeError，**绝不把不可解析路径递给引擎**。
#   · Tier B · 硬 fail-loud（R3 升级：原「软告警放行」已废止）：
#     入参为**短名**且全部候选解析失败 → BridgeError，**绝不把不可解析短名递给引擎**。
#     ⚠️ 可解析的合法裸短名（原值 / _PYWRAP_SHORT_NAME_PREFIXES 展开命中）
#        **照常放行** —— 本分级**不是**「裸短名一刀切拒绝」。
#     ⚠️ 预检误拒风险（如实标注 · 见报告 §21）：短名若命中「已加载但不在候选
#        前缀内」的模块（典型 = 项目侧 /Game 蓝图类裸短名）→ 会被误判不可解析 →
#        fail-loud。缓解 = 报错文本给出全路径改法（fail-loud 可恢复，硬崩不可）。
#   · 解析设施不可用（替身缺 load_class/load_object）→ 不阻断，留 warning 痕迹。

#: 短名候选模块前缀（Tier B 展开用；覆盖 21 项实测出现的模块）
_PYWRAP_SHORT_NAME_PREFIXES = (
    "/Script/Engine.",
    "/Script/CoreUObject.",
    "/Script/GameplayTags.",
    "/Script/UMG.",
    "/Script/AIModule.",
    "/Script/InputCore.",
    "/Script/EnhancedInput.",
)


def _pywrap_class_path_candidates(value: str, kind: str):
    """候选解析路径：全路径原样；短名 → 原值 + 常用模块前缀展开。"""
    if value.startswith("/"):
        return [value]
    return [value] + [p + value for p in _PYWRAP_SHORT_NAME_PREFIXES]


def _pywrap_try_load_path(path: str, kind: str):
    """单路径试解析。返回 (state, obj, how)。

    state 语义（三态，供 Tier A/B 分级判定）：
      · resolved    —— 解析到对象（引擎侧同样可命中）
      · miss        —— 解析设施可用但确实解析不到（全路径 → Tier A 硬拒）
      · unavailable —— 解析设施不可用（离线替身缺 API）→ 不阻断
    """
    unreal_mod = globals().get("unreal")
    if unreal_mod is None:
        return "unavailable", None, ""
    # 类/接口优先 load_class；枚举/结构/对象优先 load_object（两者都试，容错）
    order = ("class", "object") if kind in ("class", "interface") \
        else ("object", "class")

    # v3.2（2026-09-18 实测 P0）：load_class/load_object 对 **/Game 内容类路径**
    #   会触发真实 LoadPackage —— 非 GameThread 执行 = 引擎断言崩溃
    #   （`IsInGameThread()` fatal → 编辑器直接退出；/Script/ 引擎类路径仅内存
    #   查找不触发，所以此前未暴露）。
    #   分流：/Game/（与无前缀短名，可能解析到内容类）→ 投递 GameThread；
    #   /Script/ 引擎类（纯内存查找，安全）与 TEST_MODE 离线替身 → 调用线程直呼。
    _needs_gt = (_TEST_MODE is False) and not path.startswith("/Script/")

    def _do():
        tried = []
        for which in order:
            fn = getattr(unreal_mod,
                         "load_class" if which == "class" else "load_object", None)
            if not callable(fn):
                continue
            try:
                obj = fn(None, path)
            except Exception as e:          # 替身/引擎异常一律计「未命中」
                tried.append(f"{which}:{type(e).__name__}")
                continue
            if obj:
                return "resolved", obj, f"{which}('{path}')", tried
            tried.append(f"{which}:None")
        if not tried:
            return "unavailable", None, "", tried
        return "miss", None, ",".join(tried), tried

    if _needs_gt:
        try:
            st, obj, how, _tried = _run_on_game_thread_sync(_do, timeout=15.0)
        except BridgeError as e:
            # 仅在线（_TEST_MODE is False）可达：GT 投递超时仍 fail-loud
            # （编辑器可能卡住或有模态对话框）；离线替身不进此分支
            # （无 tick worker，走下方调用线程直呼，fake 环境无崩溃风险）。
            raise BridgeError(
                f"{e} （GT 投递超时——编辑器可能卡住或有模态对话框）",
                category=getattr(e, "category", "game_thread"),
                suggestion=getattr(e, "suggestion", "")) from e
    else:
        st, obj, how, _tried = _do()
    return st, obj, how


def _pywrap_try_asset_probe(path: str):
    """`/Game/` 资产宽松兜底：包存在即视为可解析（防误伤蓝图生成类 `*_C`）。

    返回 True / False / None（None = 探测 API 不可用）。
    """
    if not path.startswith("/Game/"):
        return None
    pkg = path.split(".")[0]
    unreal_mod = globals().get("unreal")
    lib = getattr(unreal_mod, "EditorAssetLibrary", None) if unreal_mod else None
    fn = getattr(lib, "does_asset_exist", None)
    if not callable(fn):
        return None
    try:
        return bool(fn(pkg))
    except Exception:
        return None


def _pywrap_guard_engine_class_path(value, param: str, category: str,
                                    kind: str = "class", label: str = ""):
    """引擎类/枚举/结构/接口路径**前置解析守卫**（D-1）。

    返回 `(原值, warnings)`；解析不到时：**全路径 / 短名一律 raise BridgeError**
    （R3 硬化：fail-loud，绝不把不可解析值递给引擎；可解析短名照常放行 + 零 warning）。
    分级语义见文件头的「递引擎前解析预检」小节。
    """
    raw = str(value or "").strip()
    if not raw:
        raise BridgeError(f"{param} 不可为空", category=category)
    tried = []
    infra_seen = False
    for path in _pywrap_class_path_candidates(raw, kind):
        state, _obj, how = _pywrap_try_load_path(path, kind)
        if state == "resolved":
            return raw, []
        if state == "miss":
            infra_seen = True
            tried.append(f"{path}({how})")
    if not infra_seen:
        return raw, [f"{param} 未经前置解析（load_class/load_object 不可用）: {raw}"]
    if raw.startswith("/"):
        probe = _pywrap_try_asset_probe(raw)
        if probe is True:
            return raw, [
                f"{param} 类加载器未命中，但资产存在（宽松放行）: {raw}"]
        raise BridgeError(
            f"{label or category}: {param} 无法解析为可加载的 {kind} 路径 → "
            f"拒绝调用引擎（引擎侧对不可解析路径会 check() 断言硬崩编辑器）: "
            f"{raw}",
            category=category,
            suggestion="已尝试: " + "; ".join(tried) +
                       "；请传入可加载路径（如 /Script/Module.XXX 或 "
                       "/Game/.../BPI_X.BPI_X_C）")
    # ── Tier B 硬化（T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE-R3 · maintainer授权）──
    # 口径：**递引擎前解析预检** —— 解析不到 → fail-loud，⛔ 不再原值透传引擎。
    # 事故依据（实测 · 硬崩）：raw\b5_addiface_bare_shortname.json @20:21:10
    #   add_implemented_interface(interface_class_path="NoSuchBareShortName_PywrapL2")
    #   → 旧 Tier B 软放行 → 引擎 check() 断言失败
    #     （Assertion failed: !InterfaceClassPathName.IsNull()
    #      @ BlueprintEditorUtils.cpp:6457）→ UnrealEditor 硬崩（PID 59488 死）。
    # ⚠️ **不是**「裸短名一刀切拒绝」：可解析的合法裸短名（原值或
    #    _PYWRAP_SHORT_NAME_PREFIXES 展开命中）仍在上方 `state == "resolved"`
    #    分支放行 → 正路径不受影响。
    raise BridgeError(
        f"{label or category}: {param} 为短名但无法解析为可加载的 {kind} → "
        f"拒绝调用引擎（引擎侧对不可解析短名会 check() 断言硬崩编辑器）: "
        f"{raw}；已拒绝调用引擎；请改用 "
        f"/Game/<目录>/<资产>.<资产>_C 全路径重试",
        category=category,
        suggestion="已尝试: " + "; ".join(tried) +
                   "；短名须可被 unreal.load_class/load_object 命中，"
                   "否则请改用全路径（如 /Script/Module.XXX 或 "
                   "/Game/.../BPI_X.BPI_X_C）",
    )


def _pywrap_resolve_node_class(node_class):
    """节点类解析：类对象原样返回；字符串按「全路径 → 候选包短名」解析。

    短名示例 `K2Node_IfThenElse`；全路径示例
    `/Script/BlueprintGraph.K2Node_IfThenElse`。全部失败 → fail-loud
    （错误信息列出所有尝试路径，便于定位）。
    """
    if node_class is None:
        raise BridgeError("node_class 不可为空", category="param_type")
    if not isinstance(node_class, str):
        return node_class          # 已是类对象（unreal.load_class 返回值）
    name = node_class.strip()
    if not name:
        raise BridgeError("node_class 不可为空字符串", category="param_type")
    if name.startswith("/"):
        candidates = [name]
    elif "." in name:
        candidates = ["/" + name] + [p + name for p in _NODE_CLASS_PACKAGES]
    else:
        candidates = [p + name for p in _NODE_CLASS_PACKAGES]
    tried = []
    for path in candidates:
        try:
            obj = unreal.load_class(None, path)
        except Exception as e:
            tried.append(f"{path}({type(e).__name__})")
            continue
        if obj:
            return obj
        tried.append(f"{path}(None)")
    raise BridgeError(
        f"节点类解析失败: {name}（尝试: {', '.join(tried)}）",
        category="param_type",
        suggestion="短名示例 K2Node_IfThenElse / K2Node_CallFunction；"
                   "全路径示例 /Script/BlueprintGraph.K2Node_IfThenElse",
    )


def _pywrap_require_graph_for_function(graph_name: str) -> str:
    """L1-7 前置：函数图硬约束（EventGraph 无 FunctionEntry → 必然静默失败）。"""
    gname = graph_name or ""
    if not gname:
        raise BridgeError(
            "graph_name 必填（L1-7 要求函数图，不是 EventGraph）",
            category="param_type",
            suggestion="函数图名示例: 由 BlueprintEditorLibrary.add_function_graph 创建的函数名",
        )
    if gname == "EventGraph":
        raise BridgeError(
            "graph_name=EventGraph 不支持函数参数/局部变量（需函数图）",
            category="param_type",
            suggestion="先用 BlueprintEditorLibrary.add_function_graph(bp, 'FnName') 建函数图",
        )
    return gname


def _pywrap_camel_to_snake(name: str) -> str:
    """CamelCase → snake_case（CDO 属性回读用：InitialLifeSpan → initial_life_span）。"""
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and not name[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _pywrap_read_cdo_property(bp, property_name: str):
    """回读 CDO 属性 → (value, err_str)。err 非空 = 回读不可用（不阻断）。"""
    try:
        gen = bp.generated_class()
    except Exception as e:
        return None, f"generated_class 异常: {e}"
    try:
        cdo = unreal.get_default_object(gen)
    except Exception as e:
        return None, f"get_default_object 异常: {e}"
    if cdo is None:
        return None, "get_default_object 返回 None"
    prop = _pywrap_camel_to_snake(property_name)
    try:
        return cdo.get_editor_property(prop), ""
    except Exception as e:
        return None, f"get_editor_property('{prop}') 异常: {e}"


def _pywrap_value_matches(expected: str, actual) -> bool:
    """值比对（数值容差 1e-6，其余按字符串包含/相等）。"""
    if actual is None:
        return False
    try:
        return abs(float(expected) - float(actual)) < 1e-6
    except Exception:
        pass
    e, a = str(expected), str(actual)
    return a == e or e in a


def _pywrap_node_result(node, graph, graph_name: str, warnings=None,
                        extra=None) -> dict:
    """建节点类命令统一返回结构（含 GUID 锚点 + 引脚 + 回读状态）。"""
    res = {
        "ok": True,
        "node_id": _pywrap_node_guid(node),
        "title": _pywrap_node_title(node),
        "graph": graph_name,
        "pins": _pywrap_read_pin_names(node),
        "warnings": list(warnings or []),
    }
    if extra:
        res.update(extra)
    return res


def _pywrap_verify_pins(node, label: str, expect_any=None, min_count: int = 0):
    """回读引脚并校验 → (pins, warnings)。

    能读到引脚就严格校验（缺引脚 / 数量不足 → fail-loud）；读不到（list_pins
    返回空 = 回读通道不可用）只记 warning 不阻断 —— 见 `_pywrap_readback_nodes`
    的 R1 回读降级策略说明。
    """
    pins = _pywrap_read_pin_names(node)
    if not pins:
        return pins, [f"{label}: 引脚回读不可用（list_pins 返回空）"]
    if min_count and len(pins) < min_count:
        raise BridgeError(
            f"{label} 引脚数不足: 期望 >= {min_count}，实读 {len(pins)}: {pins}",
            category="pin_failure",
        )
    if expect_any and not any(e in pins for e in expect_any):
        raise BridgeError(
            f"{label} 回读缺失期望引脚 {list(expect_any)}，实读: {pins}",
            category="pin_failure",
        )
    return pins, []


def _pywrap_read_local_variable_names(graph):
    """回读函数图局部变量名 → (names | None, err)。

    `None` = 回读通道不可用（UE 未把 `UK2Node_FunctionEntry::LocalVariables`
    暴露给 Python）→ 调用方记 warning，不阻断（避免假阴性）。
    """
    try:
        nodes = bridge.list_nodes(graph)
    except Exception as e:
        return None, f"list_nodes 异常: {e}"
    for n in (nodes or []):
        for attr in ("local_variables", "LocalVariables"):
            try:
                lv = getattr(n, attr, None)
            except Exception:
                continue
            if lv is None:
                continue
            names = []
            for v in lv:
                nm = None
                for vattr in ("var_name", "VarName"):
                    try:
                        nm = getattr(v, vattr, None)
                    except Exception:
                        nm = None
                    if nm:
                        break
                names.append(str(nm) if nm else str(v))
            return names, ""
    return None, "local_variables 未暴露（回读通道不可用）"


# ── L1-1 · create_node_by_class（AddNodeByClass）──

def _cmd_create_node_by_class(bp_path: str, graph_name: str = "EventGraph",
                              node_class: str = "", node_pos=None) -> dict:
    """L1-1 通用建节点：任意 UEdGraphNode 子类一次性落图。

    契约（评估单 §3.1 L1-1）：
      - `node_class` 可为类对象或类名字符串（短名 / 全路径），内部解析
      - 底层返回 None（C++ nullptr）→ **raise**（fail-loud，禁静默）
      - 建后按 GUID 回读节点，位置不一致 → **raise**（防「建了但没落图」）
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("graph_name", graph_name)
    x, y = _pywrap_pos(node_pos)
    cls_obj = _pywrap_resolve_node_class(node_class)

    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, graph_name)
    # H-1：操作前 GUID 快照（DLL 返回的裸 K2Node 取不到 NodeGuid → 用差集定位）
    before_guids, snap_err = _pywrap_snapshot_guids(graph)
    try:
        node = _safe_call(bridge.add_node_by_class, graph, cls_obj, x, y,
                          error_cat="node_add")
    except BridgeError as e:
        # H-2：DLL 可能已落节点后才失败 → 反查并清理残留
        _pywrap_fail_with_cleanup(graph, bp, before_guids,
                                  "create_node_by_class", e)
    if node is None:
        _pywrap_fail_with_cleanup(
            graph, bp, before_guids, "create_node_by_class",
            BridgeError(
                f"AddNodeByClass 返回 None（建节点失败）: class={node_class} "
                f"graph={graph_name}",
                category="node_add",
                suggestion="节点类可能不可实例化 / 抽象类，或 graph 非法"))
    warnings = []
    if snap_err:
        warnings.append(f"操作前节点快照不可用（{snap_err}）")
    new_infos, w0 = _pywrap_locate_new_nodes(graph, before_guids)
    warnings.extend(w0)
    info, w1 = _pywrap_pick_new_node(new_infos, expect_pos=(x, y),
                                     label="create_node_by_class")
    warnings.extend(w1)
    guid = info.guid if info is not None else _pywrap_node_guid(node)
    if not guid:
        warnings.append("节点 GUID 回读不可用（L1-3 锚点不可用）")
    elif node_pos is not None and (info.pos_x, info.pos_y) != (x, y):
        # M-1：「建了但落错位置」守卫（在线取到真 GUID 后才真正生效）
        # N-1：建节点后判定 → 走统一清理出口（失败即清残留，防 ghost）
        _pywrap_fail_with_cleanup(
            graph, bp, before_guids, "create_node_by_class",
            BridgeError(
                f"建节点位置回读不一致: 期望({x},{y}) 实读"
                f"({info.pos_x},{info.pos_y})",
                category="node_add",
            ))
    pins, pw = _pywrap_verify_pins(node, "create_node_by_class")
    warnings.extend(pw)
    node = None  # 生命周期契约：不跨调用持有节点引用（只用 GUID 锚点）
    return {"ok": True, "node_id": guid, "graph": graph_name,
            "pos": [x, y], "pins": pins, "warnings": warnings}


# ── L1-2 · set_node_position（SetNodePosition）──

def _cmd_set_node_position(bp_path: str, graph_name: str = "EventGraph",
                           node_guid: str = "", x: int = 0,
                           y: int = 0) -> dict:
    """L1-2 设置节点坐标（SetNodePosition）+ **回读位置**。

    契约：`ret is None/False` → raise；回读到位置且不一致 → raise
    （回归曾因残留节点误判 FAIL → 断言须唯一锚点 GUID）。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("graph_name", graph_name)
    _pywrap_require_str("node_guid", node_guid)
    x, y = _pywrap_pos([x, y])

    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, graph_name)
    gid = _pywrap_norm_guid(node_guid)
    node = _safe_call(bridge.get_node_by_guid, graph, gid,
                      error_cat="node_lookup")
    if node is None:
        raise BridgeError(
            f"节点未找到（GUID）: {node_guid}", category="node_failure")
    ret = _safe_call(bridge.set_node_position, node, x, y,
                     error_cat="node_position")
    if ret is None or ret is False:
        raise BridgeError(
            f"SetNodePosition 返回 {ret!r}（失败）: guid={gid} → ({x},{y})",
            category="node_position",
        )
    warnings = []
    info, err = _pywrap_find_info(graph, gid)
    if info is None:
        warnings.append(f"位置回读未命中（{err or 'list_nodes 无该 GUID'}）")
    elif (info.pos_x, info.pos_y) != (x, y):
        raise BridgeError(
            f"位置回读不一致: 期望({x},{y}) 实读({info.pos_x},{info.pos_y})",
            category="node_position",
        )
    node = None
    return {"ok": True, "node_id": gid, "pos": [x, y],
            "readback": [info.pos_x, info.pos_y] if info else None,
            "warnings": warnings}


# ── L1-3 · get_node_by_guid（GetNodeByGuid）──

def _cmd_get_node_by_guid(bp_path: str, graph_name: str = "EventGraph",
                          node_guid: str = "") -> dict:
    """L1-3 按 GUID 精确取节点（被污染的图里唯一可靠锚点）。

    契约：返回 None → raise（guid 不存在 / graph 不符）。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("graph_name", graph_name)
    _pywrap_require_str("node_guid", node_guid)

    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, graph_name)
    gid = _pywrap_norm_guid(node_guid)
    node = _safe_call(bridge.get_node_by_guid, graph, gid,
                      error_cat="node_lookup")
    if node is None:
        raise BridgeError(
            f"节点未找到（GUID 不存在或 graph 不符）: {node_guid} @ {graph_name}",
            category="node_failure",
        )
    pins = _pywrap_read_pin_names(node)
    # H-1：裸 K2Node 的 NodeTitle/NodeClass 在线不可达 → 改用 list_nodes 的
    #      FBPNodeInfo（同 GUID）回填 title / node_class
    info, ierr = _pywrap_find_info(graph, gid)
    warnings = []
    if info is None:
        warnings.append(
            f"节点信息回读未命中（{ierr or 'list_nodes 无该 GUID'}）")
    title = info.title if info is not None else _pywrap_node_title(node)
    node_class = (info.node_class if info is not None
                  else str(getattr(node, "node_class", "?")))
    result = {"ok": True, "node_id": gid, "graph": graph_name,
              "title": title, "node_class": node_class,
              "pins": pins, "warnings": warnings}
    node = None
    return result


# ── L1-4 · add_variable_node（AddVariableNode）──

def _cmd_add_variable_node(bp_path: str, graph_name: str = "EventGraph",
                           var_name: str = "", b_is_setter=True,
                           node_pos=None) -> dict:
    """L1-4 变量 Get/Set 节点（VarName 须**已存在**于蓝图，否则空节点）。

    契约：返回 None → raise；回读引脚须含 var_name（缺失 → raise）。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("graph_name", graph_name)
    _pywrap_require_str("var_name", var_name)
    is_setter = _pywrap_bool("b_is_setter", b_is_setter)
    x, y = _pywrap_pos(node_pos)

    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, graph_name)
    before_guids, snap_err = _pywrap_snapshot_guids(graph)
    try:
        node = _safe_call(bridge.add_variable_node, graph, var_name, is_setter,
                          x, y, error_cat="node_add")
    except BridgeError as e:
        _pywrap_fail_with_cleanup(graph, bp, before_guids,
                                  "add_variable_node", e)
    if node is None:
        _pywrap_fail_with_cleanup(
            graph, bp, before_guids, "add_variable_node",
            BridgeError(
                f"AddVariableNode 返回 None（失败）: var={var_name} "
                f"setter={is_setter}",
                category="node_add",
                suggestion="确认 var_name 已通过 add_variable 写在蓝图上"))
    pins, pw = _pywrap_verify_pins(node, "add_variable_node")
    warnings = list(pw)
    if snap_err:
        warnings.append(f"操作前节点快照不可用（{snap_err}）")
    if pins and var_name not in pins:
        # N-1：建节点后判定 → 走统一清理出口（失败即清残留，防 ghost）
        _pywrap_fail_with_cleanup(
            graph, bp, before_guids, "add_variable_node",
            BridgeError(
                f"变量节点回读缺失变量引脚 '{var_name}'，实读: {pins}",
                category="pin_failure",
            ))
    new_infos, w0 = _pywrap_locate_new_nodes(graph, before_guids)
    warnings.extend(w0)
    info, w1 = _pywrap_pick_new_node(new_infos, expect_pos=(x, y),
                                     label="add_variable_node")
    warnings.extend(w1)
    guid = info.guid if info is not None else _pywrap_node_guid(node)
    if not guid:
        warnings.append("节点 GUID 回读不可用（L1-3 锚点不可用）")
    node = None
    return {"ok": True, "node_id": guid, "graph": graph_name,
            "var_name": var_name, "b_is_setter": is_setter,
            "pins": pins, "warnings": warnings}


# ── L1-5 · add_custom_event_node（AddCustomEventNode）──

def _cmd_add_custom_event_node(bp_path: str, graph_name: str = "EventGraph",
                               event_name: str = "", node_pos=None,
                               signature_func: str = "") -> dict:
    """L1-5 自定义事件节点（跨 Actor 联动的可调用事件）。

    契约：None → raise；**须回读标题**且标题须包含 event_name
    （中文环境标题形如 `'<name>\\n自定义事件'` → 用 `in` 包含判断）。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("graph_name", graph_name)
    _pywrap_require_str("event_name", event_name)
    if signature_func:
        _pywrap_require_str("signature_func", signature_func)
    x, y = _pywrap_pos(node_pos)

    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, graph_name)
    before_guids, snap_err = _pywrap_snapshot_guids(graph)
    try:
        node = _safe_call(bridge.add_custom_event_node, graph, event_name, x, y,
                          signature_func, error_cat="node_add")
    except BridgeError as e:
        _pywrap_fail_with_cleanup(graph, bp, before_guids,
                                  "add_custom_event_node", e)
    if node is None:
        _pywrap_fail_with_cleanup(
            graph, bp, before_guids, "add_custom_event_node",
            BridgeError(
                f"AddCustomEventNode 返回 None（失败）: event={event_name}",
                category="node_add"))
    warnings = []
    if snap_err:
        warnings.append(f"操作前节点快照不可用（{snap_err}）")
    new_infos, w0 = _pywrap_locate_new_nodes(graph, before_guids)
    warnings.extend(w0)
    info, w1 = _pywrap_pick_new_node(new_infos, expect_pos=(x, y),
                                     label="add_custom_event_node")
    warnings.extend(w1)
    guid = info.guid if info is not None else _pywrap_node_guid(node)
    if not guid:
        warnings.append("节点 GUID 回读不可用（L1-3 锚点不可用）")
    # M-1：标题守卫（在线拿到真 title 后才真正生效，非空转）
    title = info.title if info is not None else _pywrap_node_title(node)
    if title and event_name not in title:
        # N-1：建节点后判定 → 走统一清理出口（失败即清残留，防 ghost）
        _pywrap_fail_with_cleanup(
            graph, bp, before_guids, "add_custom_event_node",
            BridgeError(
                f"自定义事件标题回读不符: 期望包含 '{event_name}'，实读 {title!r}",
                category="node_add",
            ))
    if not title:
        warnings.append("自定义事件标题回读不可用")
    pins, pw = _pywrap_verify_pins(node, "add_custom_event_node")
    warnings.extend(pw)
    node = None
    return {"ok": True, "node_id": guid, "graph": graph_name, "title": title,
            "pins": pins, "warnings": warnings}


# ── L1-6 · add_input_key_event_node（AddInputKeyEventNode）──

def _cmd_add_input_key_event_node(bp_path: str, graph_name: str = "EventGraph",
                                  key_name: str = "", node_pos=None) -> dict:
    """L1-6 按键事件节点（W / A / SpaceBar / LeftMouseButton ...）。

    契约：None → raise（key 名非法）；回读引脚（读不到只告警）。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("graph_name", graph_name)
    _pywrap_require_str("key_name", key_name)
    x, y = _pywrap_pos(node_pos)

    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, graph_name)
    # H-1 快照：成功路径取真 GUID + 失败路径（H-2）反查残留节点
    before_guids, snap_err = _pywrap_snapshot_guids(graph)
    try:
        node = _safe_call(bridge.add_input_key_event_node, graph, key_name,
                          x, y, error_cat="node_add")
    except BridgeError as e:
        _pywrap_fail_with_cleanup(graph, bp, before_guids,
                                  "add_input_key_event_node", e)
    if node is None:
        # H-2：DLL「先插节点再返回 None」→ 必须反查并删除残留，
        #      否则残留非法节点会让宿主蓝图 Compile FAILED Status=5 并随存盘落盘
        _pywrap_fail_with_cleanup(
            graph, bp, before_guids, "add_input_key_event_node",
            BridgeError(
                f"AddInputKeyEventNode 返回 None（失败）: key={key_name}",
                category="node_add",
                suggestion="key_name 需为合法 FKey 名（W / A / SpaceBar / "
                           "LeftMouseButton / RightMouseButton ...）"))
    pins, pw = _pywrap_verify_pins(node, "add_input_key_event_node")
    warnings = list(pw)
    if snap_err:
        warnings.append(f"操作前节点快照不可用（{snap_err}）")
    new_infos, w0 = _pywrap_locate_new_nodes(graph, before_guids)
    warnings.extend(w0)
    info, w1 = _pywrap_pick_new_node(new_infos, expect_pos=(x, y),
                                     label="add_input_key_event_node")
    warnings.extend(w1)
    guid = info.guid if info is not None else _pywrap_node_guid(node)
    if not guid:
        warnings.append("节点 GUID 回读不可用（L1-3 锚点不可用）")
    node = None
    return {"ok": True, "node_id": guid, "graph": graph_name,
            "key_name": key_name, "pins": pins, "warnings": warnings}


# ── L1-7a · add_function_pin（AddFunctionPinEntry）──

def _cmd_add_function_pin(bp_path: str, graph_name: str = "",
                          pin_name: str = "", pin_type: str = "",
                          b_is_input=True) -> dict:
    """L1-7a 函数图参数引脚（graph 须为**函数图**）。

    语义（C++ 头文件 B-6a）：`bIsInput=True` → FunctionEntry 入参；
    `False` → FunctionResult 出参。
    契约：ret None/False → raise；回读须含 pin_name（读不到只告警）。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("pin_name", pin_name)
    _pywrap_require_str("pin_type", pin_type)
    is_input = _pywrap_bool("b_is_input", b_is_input)
    gname = _pywrap_require_graph_for_function(graph_name)

    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, gname)
    ret = _safe_call(bridge.add_function_pin_entry, graph, pin_name, pin_type,
                     is_input, error_cat="pin_add")
    if ret is None or ret is False:
        raise BridgeError(
            f"AddFunctionPinEntry 返回 {ret!r}（失败）: {pin_name}:{pin_type} "
            f"is_input={is_input} @ {gname}",
            category="pin_add",
        )
    # M-2：list_nodes 返回 FBPNodeInfo（USTRUCT，非节点）→ 直接喂 list_pins
    #      会被 UE 报 Cannot nativize 'BPNodeInfo' as 'Node'（假阴性）。
    #      改为 get_node_by_guid 换回真实 UEdGraphNode* 后再读引脚。
    found = False
    warnings = []
    real_nodes = _pywrap_iter_real_nodes(graph)
    if not real_nodes:
        warnings.append("引脚回读不可用（未取到真实 UEdGraphNode 对象）")
    else:
        for n in real_nodes:
            try:
                if pin_name in _pywrap_read_pin_names(n):
                    found = True
                    break
            except Exception as e:
                warnings.append(f"引脚回读不可用: {e}")
                break
    if not found and not warnings:
        warnings.append(
            f"函数图引脚回读未命中 '{pin_name}'（可能回读通道不可用）")
    return {"ok": True, "graph": gname, "pin_name": pin_name,
            "pin_type": pin_type, "b_is_input": is_input,
            "readback": found, "warnings": warnings}


# ── L1-7b · add_local_variable（AddLocalVariable）──

def _cmd_add_local_variable(bp_path: str, graph_name: str = "",
                            var_name: str = "", var_type: str = "") -> dict:
    """L1-7b 函数图局部变量（graph 须为**函数图**）。

    契约：ret None/False → raise；回读局部变量名（通道可用且未命中 → raise；
    通道不可用 → warning + readback=None，避免假阴性）。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("var_name", var_name)
    _pywrap_require_str("var_type", var_type)
    gname = _pywrap_require_graph_for_function(graph_name)

    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, gname)
    ret = _safe_call(bridge.add_local_variable, graph, var_name, var_type,
                     error_cat="variable_add")
    if ret is None or ret is False:
        raise BridgeError(
            f"AddLocalVariable 返回 {ret!r}（失败）: {var_name}:{var_type} "
            f"@ {gname}",
            category="variable_add",
        )
    names, err = _pywrap_read_local_variable_names(graph)
    warnings = []
    if names is None:
        warnings.append(f"局部变量回读不可用（{err}）")
        readback = None
    else:
        readback = var_name in names
        if not readback:
            raise BridgeError(
                f"局部变量回读未命中 '{var_name}'，实读: {names}",
                category="variable_add",
            )
    return {"ok": True, "graph": gname, "var_name": var_name,
            "var_type": var_type, "readback": readback, "warnings": warnings}


# ── L1-8 · add_timeline_node（AddTimelineNode）──

def _cmd_add_timeline_node(bp_path: str, graph_name: str = "EventGraph",
                           timeline_name: str = "", node_pos=None,
                           strict_pins: bool = True) -> dict:
    """L1-8 Timeline 节点（门平滑动画等）。

    契约：None → raise；回读引脚须 >= 4（防「静默空节点」，E1 类事故）；
    `strict_pins=True`（默认）→ 引脚数 != 10 直接 raise（契约引脚见
    `_TIMELINE_EXPECTED_PINS`）；如遇引擎版本引脚差异可传
    `strict_pins=false` 降级为告警。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("graph_name", graph_name)
    _pywrap_require_str("timeline_name", timeline_name)
    strict = _pywrap_bool("strict_pins", strict_pins)
    x, y = _pywrap_pos(node_pos)

    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, graph_name)
    before_guids, snap_err = _pywrap_snapshot_guids(graph)
    try:
        node = _safe_call(bridge.add_timeline_node, graph, timeline_name, x, y,
                          error_cat="node_add")
    except BridgeError as e:
        _pywrap_fail_with_cleanup(graph, bp, before_guids,
                                  "add_timeline_node", e)
    if node is None:
        _pywrap_fail_with_cleanup(
            graph, bp, before_guids, "add_timeline_node",
            BridgeError(
                f"AddTimelineNode 返回 None（失败）: timeline={timeline_name}",
                category="node_add"))
    pins, warnings = _pywrap_verify_pins(
        node, "add_timeline_node", min_count=4,
        expect_any=("Play", "Update", "Finished"))
    if pins and len(pins) != len(_TIMELINE_EXPECTED_PINS):
        msg = (f"Timeline 引脚数 {len(pins)}（契约要求 "
               f"{len(_TIMELINE_EXPECTED_PINS)}: "
               f"{'/'.join(_TIMELINE_EXPECTED_PINS)}）实读: {pins}")
        if strict:
            # N-1：建节点后判定 → 走统一清理出口（失败即清残留，防 ghost）
            _pywrap_fail_with_cleanup(
                graph, bp, before_guids, "add_timeline_node",
                BridgeError(
                    msg, category="pin_failure",
                    suggestion="确认 DLL 含 PATCH-A/B（AddTimelineNode）；如属引擎"
                               "版本引脚差异，可传 strict_pins=false 降级为告警",
                ))
        warnings.append(msg + "（strict_pins=false：仅告警）")
    if snap_err:
        warnings.append(f"操作前节点快照不可用（{snap_err}）")
    new_infos, w0 = _pywrap_locate_new_nodes(graph, before_guids)
    warnings.extend(w0)
    info, w1 = _pywrap_pick_new_node(new_infos, expect_pos=(x, y),
                                     label="add_timeline_node")
    warnings.extend(w1)
    guid = info.guid if info is not None else _pywrap_node_guid(node)
    if not guid:
        warnings.append("节点 GUID 回读不可用（L1-3 锚点不可用）")
    node = None
    return {"ok": True, "node_id": guid, "graph": graph_name,
            "timeline_name": timeline_name, "pins": pins,
            "pin_count": len(pins), "warnings": warnings}


# ── L1-9 · add_comment_node（AddCommentNode）──

def _cmd_add_comment_node(bp_path: str, graph_name: str = "EventGraph",
                          text: str = "", node_pos=None, width: int = 400,
                          height: int = 200) -> dict:
    """L1-9 注释框（图可读性 / 交付可审计）。

    契约：None → raise；**标题回读校验**——UE D-2（注释文本不写入）已由
    `T-20260910-UE5BRIDGE-D1D2-FIX` 修复（文本写入节点创建后），本封装加
    「文本须出现在回读标题中」硬校验：若回归 D-2 类缺陷 → fail-loud 而非静默。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("graph_name", graph_name)
    _pywrap_require_str("text", text)
    x, y = _pywrap_pos(node_pos)
    try:
        width, height = int(width), int(height)
    except Exception:
        raise BridgeError(
            f"width/height 必须为整数: {width!r}/{height!r}",
            category="param_type")

    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, graph_name)
    before_guids, snap_err = _pywrap_snapshot_guids(graph)
    try:
        node = _safe_call(bridge.add_comment_node, graph, text, x, y,
                          width, height, error_cat="node_add")
    except BridgeError as e:
        _pywrap_fail_with_cleanup(graph, bp, before_guids,
                                  "add_comment_node", e)
    if node is None:
        _pywrap_fail_with_cleanup(
            graph, bp, before_guids, "add_comment_node",
            BridgeError(
                f"AddCommentNode 返回 None（失败）: text={text!r}",
                category="node_add"))
    warnings = []
    if snap_err:
        warnings.append(f"操作前节点快照不可用（{snap_err}）")
    new_infos, w0 = _pywrap_locate_new_nodes(graph, before_guids)
    warnings.extend(w0)
    info, w1 = _pywrap_pick_new_node(new_infos, expect_pos=(x, y),
                                     label="add_comment_node")
    warnings.extend(w1)
    guid = info.guid if info is not None else _pywrap_node_guid(node)
    if not guid:
        warnings.append("节点 GUID 回读不可用（L1-3 锚点不可用）")
    # M-1：D-2 回归哨兵（标题为空/丢失即 fail-loud），在线拿到真 title 后生效
    title = info.title if info is not None else _pywrap_node_title(node)
    if not title:
        warnings.append("注释标题回读不可用")
    elif text not in title:
        # N-1：建节点后判定 → 走统一清理出口（失败即清残留，防 ghost）
        _pywrap_fail_with_cleanup(
            graph, bp, before_guids, "add_comment_node",
            BridgeError(
                f"注释文本未写入（D-2 类缺陷）: 期望标题包含 {text!r}，实读 {title!r}",
                category="node_add",
                suggestion="确认为 D1D2-FIX 之后的 DLL（AddCommentNode 文本写在"
                           "节点创建之后）；旧 DLL 可临时用 set_node_property"
                           "(node, 'NodeComment', text) 替代",
            ))
    node = None
    return {"ok": True, "node_id": guid, "graph": graph_name, "title": title,
            "size": [width, height], "warnings": warnings}


# ── L1-10 · set_class_default（SetClassDefault）──

def _cmd_set_class_default(bp_path: str, property_name: str = "",
                           value: str = "") -> dict:
    """L1-10 写类默认值（GeneratedClass CDO → FindFProperty + ImportText）。

    契约：ret None/False → raise；**CDO 回读验证**（通道可用且不一致 → raise）。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("property_name", property_name)
    _pywrap_require_str("value", value, allow_empty=True)

    bp = _resolve_blueprint(bp_path)
    ret = _safe_call(bridge.set_class_default, bp, property_name, value,
                     error_cat="class_default")
    if ret is None or ret is False:
        raise BridgeError(
            f"SetClassDefault 返回 {ret!r}（失败）: {property_name}={value!r}",
            category="class_default",
            suggestion="确认 property_name 是 CDO 上存在的 UPROPERTY，且 value "
                       "字符串可被 ImportText 解析（如 '123.0' / 'True'）",
        )
    readback, err = _pywrap_read_cdo_property(bp, property_name)
    warnings = []
    if err:
        warnings.append(f"CDO 回读不可用（{err}）")
        matched = None
    else:
        matched = _pywrap_value_matches(value, readback)
        if not matched:
            raise BridgeError(
                f"CDO 回读不一致: {property_name} 期望 {value!r} 实读 "
                f"{readback!r}",
                category="class_default",
            )
    bp = None
    return {"ok": True, "property_name": property_name, "value": value,
            "readback": readback, "matched": matched, "warnings": warnings}


# ══════════════════════════════════════════════════════════
#  T-20260912-UE5BRIDGE-PYWRAP-L2-IMPL · PATCH-A/B 剩余 21 项 Python 封装
#  契约（与 L1 一致，逐条见派单 §三）：
#    ① 出口唯一 —— 每个 _cmd_* 的失败路径必须走 _pywrap_fail_with_cleanup，禁裸 raise
#    ② 建节点后回读 —— GUID 归一化定位；标题定位 fail-loud（TITLEGATE）
#    ③ M-2 类型铁律 —— list_nodes 返回 FBPNodeInfo（USTRUCT，非节点）→ 需真实节点时
#       必须 get_node_by_guid 换回 UEdGraphNode*；get_pin_infos 只吃 BPNodeInfo
#    ④ 白名单 21 条逐项登记（本轮后 = 47 条）
#    ⑤ 重名/幂等语义：同名引脚 / 同名事件分发器 → fail-loud（禁止静默双建）
#  权威签名来源：UE5.1 BlueprintPythonBridge 插件 Public/.h + Private/.cpp
# ══════════════════════════════════════════════════════════

def _pywrap_l2_impl_id(guard_code=None, file_bytes=None) -> str:
    """L2 **实现指纹** —— 绑定「守卫实现 + 模块源码」，供在线 fixture 判新旧模块。

    组成（两侧可用同一公式复算，无需执行模块）::

        L2-IMPL/<guard 定义行号>/<guard co_code sha1[:12]>/<模块文件 sha1[:12]>/<文件字节数>

    用途（T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE-R4 · R5 遗留 L1）：在线用例的
    环境自愈 fixture 比对「**UE 进程内模块指纹**」vs「**磁盘模块指纹**」。
    旧判据只看「21 条 L2 命令是否在 `/command` 白名单」→ 对**只改既有分支**的硬化
    （如 R3 的 Tier B 软告警 → 硬拦）**完全看不见**：白名单一字未变，进程内却仍是旧实现。
    事故形态（R5 实测）：进程内跑旧模块 → 首条闸门按原样直跑会**复现硬崩**（报告 §16），
    当时靠人工 `importlib.reload` 绕过。

    ⚠️ 公式为**双份实现**：本函数 + `tests\test_pywrap_l2_online.py::disk_l2_impl_id`
      （离线侧用 `compile()` + 文件字节复算，不 import 模块）——**两处必须同改**；
      一致性由 `tests\test_pywrap_l2_guard.py::TestL2ImplFingerprint` 闸门守住。
    ⚠️ 纯只读：不写文件、不改状态；解析失败走保守兜底（返回带 `unavailable` 的串 → 判旧 → 热载）。
    """
    import hashlib

    try:
        if guard_code is None:
            guard_code = _pywrap_guard_engine_class_path.__code__
        lineno = int(guard_code.co_firstlineno)
        code_sig = hashlib.sha1(guard_code.co_code).hexdigest()[:12]
    except Exception as e:                        # pragma: no cover - 兜底
        lineno, code_sig = -1, "code-unavailable-%s" % type(e).__name__
    try:
        if file_bytes is None:
            with open(__file__, "rb") as fh:
                file_bytes = fh.read()
        # 换行归一化：指纹跟踪**实现内容**而非 EOL——CRLF/LF 检出差异
        # （Windows autocrlf / 复制工具）不得造成误报（2026-09-19 CI 实测踩中）
        file_bytes = file_bytes.replace(b"\r\n", b"\n")
        file_sig = hashlib.sha1(file_bytes).hexdigest()[:12]
        file_len = len(file_bytes)
    except Exception as e:                        # pragma: no cover - 兜底
        file_sig, file_len = "file-unavailable-%s" % type(e).__name__, -1
    return "L2-IMPL/%d/%s/%s/%d" % (lineno, code_sig, file_sig, file_len)


#: L2 实现指纹（import 时计算；在线用例据此判定进程内模块是否陈旧）
_PYWRAP_L2_IMPL_ID = _pywrap_l2_impl_id()

#: Switch 节点合法 TypeKind（C++ AddSwitchNode 校验，见 cpp:2623）
_SWITCH_TYPE_KINDS = ("int", "string", "name", "enum")


def _pywrap_require_node(graph, node_guid, label: str):
    """node_guid → 真实 UEdGraphNode*（M-2：禁把 FBPNodeInfo 当节点用）。

    返回 (归一化 GUID, node)；GUID 空 / 未命中 → fail-loud。
    """
    gid = _pywrap_norm_guid(node_guid)
    if not gid:
        raise BridgeError(
            f"{label}: node_guid 为空（需 32 位 hex 或可归一化 GUID 串）",
            category="param_type")
    node = _safe_call(bridge.get_node_by_guid, graph, gid,
                      error_cat="node_lookup")
    if node is None:
        raise BridgeError(
            f"{label}: 节点未找到（GUID 不存在或 graph 不符）: {node_guid}",
            category="node_failure")
    return gid, node


def _pywrap_short_class_name(path: str) -> str:
    """类 / 枚举 / 接口路径 → 短名。

    `/Script/Engine.Actor` → `Actor`；`/Game/Test/BPI_Foo.BPI_Foo_C` → `BPI_Foo`。
    """
    last = str(path or "").rstrip("/").split("/")[-1]
    if "." in last:
        last = last.split(".")[-1]
    if last.endswith("_C"):
        last = last[:-2]
    return last


def _pywrap_read_node_property(node, property_name: str):
    """回读节点属性 → (value | None, err_str)。err 非空 = 回读通道不可用（不阻断）。

    R1 回读降级策略（同 `_pywrap_readback_nodes`）：能读到 → 严格比对；
    读不到 → 仅 warning（避免把「读不到」当「操作失败」制造假阴性）。
    """
    snake = _pywrap_camel_to_snake(property_name)
    for attr in (snake, property_name):
        getter = getattr(node, "get_editor_property", None)
        if callable(getter):
            try:
                return getter(attr), ""
            except Exception:
                pass
        try:
            return getattr(node, attr), ""
        except Exception:
            continue
    return None, f"节点属性 {property_name!r} 回读通道不可用"


def _pywrap_read_component_names(bp):
    """回读 SCS 组件名清单 → (names | None, err)。

    `None` = 回读通道不可用（不阻断，只告警）。兼容：BPGC.component_templates
    （C++ SCS 添加后编译填充）。D-1 关联：组件接口一律「先回读校验、再
    fail-loud」，不把 SCS 外链断的引擎断言崩溃留给调用方。
    """
    try:
        gen = bp.generated_class()
    except Exception as e:
        return None, f"generated_class 异常: {e}"
    if gen is None:
        return None, "generated_class 返回 None"
    try:
        tpls = gen.component_templates
    except Exception:
        return None, "component_templates 未暴露（回读通道不可用）"
    if tpls is None:
        return None, "component_templates 为 None"
    names = []
    for t in tpls:
        nm = None
        getter = getattr(t, "get_name", None)
        if callable(getter):
            try:
                nm = getter()
            except Exception:
                nm = None
        if not nm:
            nm = (getattr(t, "component_name", None)
                  or getattr(t, "_name", None))
        names.append(str(nm) if nm else str(t))
    return names, ""


def _pywrap_read_implemented_interfaces(bp):
    """回读蓝图已实现接口名 → (names | None, err)。None = 通道不可用（只告警）。"""
    try:
        gen = bp.generated_class()
    except Exception as e:
        return None, f"generated_class 异常: {e}"
    if gen is None:
        return None, "generated_class 返回 None"
    try:
        ifaces = gen.implemented_interfaces
    except Exception:
        return None, "implemented_interfaces 未暴露（回读通道不可用）"
    if ifaces is None:
        return None, "implemented_interfaces 为 None"
    out = []
    for i in ifaces:
        getter = getattr(i, "get_name", None)
        if callable(getter):
            try:
                out.append(str(getter()))
                continue
            except Exception:
                pass
        out.append(str(i))
    return out, ""


def _pywrap_read_delegate_graphs(bp):
    """回读事件分发器签名图名 → (names | None, err)。None = 通道不可用（只告警）。"""
    graphs = None
    for attr in ("delegate_signature_graphs", "DelegateSignatureGraphs"):
        try:
            graphs = getattr(bp, attr)
        except Exception:
            graphs = None
        if graphs is not None:
            break
    if graphs is None:
        return None, "delegate_signature_graphs 未暴露（回读通道不可用）"
    out = []
    for g in graphs:
        getter = getattr(g, "get_name", None)
        if callable(getter):
            try:
                out.append(str(getter()))
                continue
            except Exception:
                pass
        out.append(str(g))
    return out, ""


def _pywrap_out_str_result(result, op: str, subject: str = ""):
    """解析「bool + FString& OutError」C++ 签名的 UE Python 反射返回值。

    引擎规则（PyGenUtil.cpp PackReturnValues；T-20260822 实测 + 引擎源码实锤）：
      · bool=false → 返回 None（OutError 被吞，文本不可达）
      · bool=true  → 返回 OutError 值（成功 '' / 失败 非空错误文本）
    → '' 空字符串 = 成功；None = 失败（无文本）；非空 str = 失败 + 错误文本。
    返回 (ok: bool, text: str, err: str|None)；不抛异常，由调用方 fail-loud。
    """
    tail = (" " + subject) if subject else ""
    if result is None or result is False:
        return (False, "",
                f"{op} 返回 {result!r}（失败；错误文本被 UE 反射吞掉，"
                f"详见 UE 日志）{tail}")
    if isinstance(result, str):
        if result:
            return False, result, f"{op} 失败: {result}{tail}"
        return True, "", None
    return True, "", None


def _pywrap_ctor_result(node, graph, bp, graph_name, x, y, label,
                        before_guids, snap_err, extra_warnings=None):
    """建节点类命令统一收尾：引脚回读 + 快照差集取 GUID + 统一返回结构。

    ⚠️ 失败路径一律走 `_pywrap_fail_with_cleanup`（出口唯一，防 ghost 残留）。
    """
    try:
        pins, pw = _pywrap_verify_pins(node, label)
    except BridgeError as e:
        _pywrap_fail_with_cleanup(graph, bp, before_guids, label, e)
    w = list(pw)
    if extra_warnings:
        w.extend(extra_warnings)
    if snap_err:
        w.append(f"操作前节点快照不可用（{snap_err}）")
    new_infos, w0 = _pywrap_locate_new_nodes(graph, before_guids)
    w.extend(w0)
    info, w1 = _pywrap_pick_new_node(new_infos, expect_pos=(x, y), label=label)
    w.extend(w1)
    guid = info.guid if info is not None else _pywrap_node_guid(node)
    if not guid:
        w.append("节点 GUID 回读不可用（L1-3 锚点不可用）")
    title = info.title if info is not None else _pywrap_node_title(node)
    return {"ok": True, "node_id": guid, "graph": graph_name, "title": title,
            "pins": pins, "warnings": w}


def _pywrap_make_node(bp_path, graph_name, node_pos, label, invoke,
                      require_label=None, suggestion="", pre_warnings=None):
    """通用建节点命令（节点构造 14 项共用）。

    `invoke(graph, x, y) → node`；返回 None（C++ nullptr）→ 统一清理出口
    fail-loud（**禁止静默**，N-1 教训：DLL 可能已插节点后才返回失败）。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("graph_name", graph_name)
    x, y = _pywrap_pos(node_pos)
    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, graph_name)
    before_guids, snap_err = _pywrap_snapshot_guids(graph)
    try:
        node = _safe_call(invoke, graph, x, y, error_cat="node_add")
    except BridgeError as e:
        _pywrap_fail_with_cleanup(graph, bp, before_guids, label, e)
    if node is None:
        _pywrap_fail_with_cleanup(
            graph, bp, before_guids, label,
            BridgeError(
                f"{require_label or label} 返回 None（建节点失败）",
                category="node_add", suggestion=suggestion))
    return _pywrap_ctor_result(node, graph, bp, graph_name, x, y, label,
                               before_guids, snap_err,
                               extra_warnings=pre_warnings)

# ── L2-01 · add_pin_to_node（AddPinToNode · 动态引脚）──

def _cmd_add_pin_to_node(bp_path: str, graph_name: str = "EventGraph",
                         node_guid: str = "", direction: str = "input",
                         pin_type: str = "", pin_name: str = "") -> dict:
    """L2-01 动态给任意节点加引脚（AddPinToNode）。

    C++（cpp:2876）：`bool AddPinToNode(UEdGraphNode*, FString Direction,
      FString PinTypeStr, FString PinName, FString& OutPinName)`。
    语义：bool=true → Python 收到 OutPinName（实际引脚名）；bool=false → None。
    重名语义（契约 ⑤）：同名引脚已存在 → C++ 已 fail（"pin already exists"）→
      本封装**前置 fail-loud**，禁静默双建；幂等 = 重复调用第二次必失败。
    pin_type 语法（cpp BridgeParsePinType）：exec|bool|int|int64|float|double|
      string|name|text|wildcard|object:<Class>|class:<Class>|struct:<Struct> 等。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("graph_name", graph_name)
    _pywrap_require_str("pin_name", pin_name)
    _pywrap_require_str("pin_type", pin_type)
    _pywrap_require_str("direction", direction)
    d = direction.strip().lower()
    if d not in ("input", "output"):
        raise BridgeError(
            f"add_pin_to_node: direction 非法 {direction!r}（仅 input|output）",
            category="param_type")
    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, graph_name)
    gid, node = _pywrap_require_node(graph, node_guid, "add_pin_to_node")
    existing = _pywrap_read_pin_names(node)
    if pin_name in existing:
        raise BridgeError(
            f"add_pin_to_node: 引脚 '{pin_name}' 已存在于节点 {gid}（禁静默双建）",
            category="pin_add",
            suggestion="换 pin_name，或先用 remove_pin_from_node 删旧引脚")
    result = _safe_call(bridge.add_pin_to_node, node, d, pin_type, pin_name,
                        error_cat="pin_add")
    # ⚠️ 本接口的 FString& 出参语义与 OutError **相反**：非空 = 成功（实际引脚名），
    #    与 `_pywrap_out_str_result`（非空 = 错误）不可混用。
    if result is None or result is False or result == "":
        raise BridgeError(
            f"AddPinToNode 返回 {result!r}（失败）: {pin_name}:{pin_type} "
            f"direction={d} @ {gid}",
            category="pin_add",
            suggestion="direction ∈ {input,output}；pin_type 见 cpp "
                       "BridgeParsePinType（exec/bool/int/float/string/"
                       "object:<Class>/struct:<Struct> ...）")
    out_name = str(result)
    pins = _pywrap_read_pin_names(node)
    warnings = []
    if not pins:
        warnings.append("引脚回读不可用（list_pins 返回空）")
    elif out_name not in pins:
        warnings.append(
            f"新引脚 {out_name!r} 未出现在回读清单（list_pins 通道可能滞后）")
    node = None
    return {"ok": True, "node_id": gid, "pin_name": pin_name,
            "out_pin_name": out_name, "direction": d, "pin_type": pin_type,
            "pins": pins, "warnings": warnings}


# ── L2-02 · remove_pin_from_node（RemovePinFromNode）──

def _cmd_remove_pin_from_node(bp_path: str, graph_name: str = "EventGraph",
                              node_guid: str = "", pin_name: str = "") -> dict:
    """L2-02 按引脚名移除节点引脚（RemovePinFromNode）→ bool。

    C++（cpp:2954）：Node null / PinName 空 / 无该引脚 / RemovePin 拒绝 → false。
    回读校验：能读到引脚清单且 pin_name 仍在 → fail-loud（移除未生效）。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("graph_name", graph_name)
    _pywrap_require_str("pin_name", pin_name)
    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, graph_name)
    gid, node = _pywrap_require_node(graph, node_guid, "remove_pin_from_node")
    before = _pywrap_read_pin_names(node)
    if before and pin_name not in before:
        raise BridgeError(
            f"remove_pin_from_node: 节点 {gid} 上无引脚 '{pin_name}'，实读 {before}",
            category="pin_remove")
    result = _safe_call(bridge.remove_pin_from_node, node, pin_name,
                        error_cat="pin_remove")
    if result is None or result is False:
        raise BridgeError(
            f"RemovePinFromNode 返回 {result!r}（失败）: pin={pin_name} @ {gid}",
            category="pin_remove",
            suggestion="确认引脚名精确匹配；已连线引脚可能被引擎拒绝移除")
    after = _pywrap_read_pin_names(node)
    warnings = []
    if not after:
        warnings.append("引脚回读不可用（list_pins 返回空）")
        removed = None
    else:
        removed = pin_name not in after
        if not removed:
            raise BridgeError(
                f"引脚移除后仍在回读清单中: '{pin_name}'，实读 {after}",
                category="pin_remove")
    node = None
    return {"ok": True, "node_id": gid, "pin_name": pin_name,
            "removed": removed, "pins": after, "warnings": warnings}


# ── L2-03 · try_node_add_input_pin（TryNodeAddInputPin）──

def _cmd_try_node_add_input_pin(bp_path: str, graph_name: str = "EventGraph",
                                node_guid: str = "", strict: bool = True) -> dict:
    """L2-03 节点语义层加输入引脚（TryNodeAddInputPin）→ bool。

    C++（cpp:2994）：仅实现 IK2Node_AddPinInterface 的节点可用（Switch / Select /
      FormatText）；节点未实现接口 / CanAddPin() 为假 → false → fail-loud。
    `strict=True`（默认）：返回 true 但引脚数未增加 → fail-loud（防静默空转）；
    `strict=False` → 降级为告警（对齐 add_timeline_node 既有约定）。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("graph_name", graph_name)
    strict = _pywrap_bool("strict", strict)
    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, graph_name)
    gid, node = _pywrap_require_node(graph, node_guid,
                                     "try_node_add_input_pin")
    before = _pywrap_read_pin_names(node)
    result = _safe_call(bridge.try_node_add_input_pin, node, error_cat="pin_add")
    if result is None or result is False:
        raise BridgeError(
            f"TryNodeAddInputPin 返回 {result!r}（失败）: node={gid} 未实现 "
            f"IK2Node_AddPinInterface 或 CanAddPin() 为假",
            category="pin_add",
            suggestion="仅 Switch / Select / FormatText 等支持动态加引脚的节点可用")
    after = _pywrap_read_pin_names(node)
    warnings = []
    added = None
    if not before or not after:
        warnings.append("引脚回读不可用（list_pins 返回空）")
    else:
        added = len(after) > len(before)
        msg = (f"TryNodeAddInputPin 返回 True 但引脚数未增加"
               f"（before={len(before)} after={len(after)}）")
        if not added and strict:
            raise BridgeError(msg, category="pin_add")
        if not added:
            warnings.append(msg + "（strict=false：仅告警）")
    node = None
    return {"ok": True, "node_id": gid, "pin_count": len(after),
            "before_count": len(before), "added": added,
            "pins": after, "warnings": warnings}


# ── L2-04 · set_node_property（SetNodeProperty）──

def _cmd_set_node_property(bp_path: str, graph_name: str = "EventGraph",
                           node_guid: str = "", property_name: str = "",
                           value: str = "") -> dict:
    """L2-04 反射写节点属性（SetNodeProperty）→ bool。

    C++（cpp:2042）：FindFProperty(Node->GetClass()) + ImportText_Direct。
    典型属性：EnabledState / NodeComment / bCommentBubbleVisible /
      AdvancedPinDisplay（属性名用 C++ 原名）。
    回读校验（R1 降级）：通道可用且不一致 → fail-loud；不可用 → 仅告警。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("graph_name", graph_name)
    _pywrap_require_str("property_name", property_name)
    _pywrap_require_str("value", value, allow_empty=True)
    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, graph_name)
    gid, node = _pywrap_require_node(graph, node_guid, "set_node_property")
    ret = _safe_call(bridge.set_node_property, node, property_name, value,
                     error_cat="node_property")
    if ret is None or ret is False:
        raise BridgeError(
            f"SetNodeProperty 返回 {ret!r}（失败）: {property_name}={value!r} "
            f"@ {gid}",
            category="node_property",
            suggestion="确认 property_name 是节点类上存在的 UPROPERTY（C++ 原名，"
                       "如 EnabledState / NodeComment），且 value 可被 ImportText "
                       "解析")
    readback, rerr = _pywrap_read_node_property(node, property_name)
    warnings = []
    if rerr:
        warnings.append(f"节点属性回读不可用（{rerr}）")
        matched = None
    else:
        matched = _pywrap_value_matches(value, readback)
        if not matched:
            raise BridgeError(
                f"节点属性回读不一致: {property_name} 期望 {value!r} 实读 "
                f"{readback!r}",
                category="node_property")
    node = None
    return {"ok": True, "node_id": gid, "property_name": property_name,
            "value": value, "readback": readback, "matched": matched,
            "warnings": warnings}

# ── L2-05 · add_cast_node（AddCastNode）──

def _cmd_add_cast_node(bp_path: str, graph_name: str = "EventGraph",
                       target_class_name: str = "", node_pos=None) -> dict:
    """L2-05 动态 Cast 节点（AddCastNode → UK2Node_DynamicCast）。

    C++（cpp:2174）：TargetClassName 空 / 类不可解析（FindObject+LoadObject 双试）
      → nullptr。
    target_class_name 支持 `/Script/Engine.Actor` 与 `/Game/.../BP_X.BP_X_C`。
    """
    _pywrap_require_str("target_class_name", target_class_name)
    target_class_name, _w_guard = _pywrap_guard_engine_class_path(
        target_class_name, "target_class_name", "param_type",
        kind="class", label="add_cast_node")
    return _pywrap_make_node(
        bp_path, graph_name, node_pos, "add_cast_node",
        lambda graph, x, y: bridge.add_cast_node(
            graph, target_class_name, x, y),
        require_label="AddCastNode",
        pre_warnings=_w_guard,
        suggestion="target_class_name 需可被解析（/Script/Engine.Actor 或 "
                   "/Game/.../BP_X.BP_X_C）")


# ── L2-06 · add_class_cast_node（AddClassCastNode）──

def _cmd_add_class_cast_node(bp_path: str, graph_name: str = "EventGraph",
                             target_class_name: str = "", node_pos=None) -> dict:
    """L2-06 Class 动态 Cast（AddClassCastNode → UK2Node_ClassDynamicCast）。

    C++（cpp:3607）：TargetClassName 空 / 类不可解析 → nullptr。
    与 add_cast_node 的区别：对**类引用**做 Cast（类→子类），不实例。
    """
    _pywrap_require_str("target_class_name", target_class_name)
    target_class_name, _w_guard = _pywrap_guard_engine_class_path(
        target_class_name, "target_class_name", "param_type",
        kind="class", label="add_class_cast_node")
    return _pywrap_make_node(
        bp_path, graph_name, node_pos, "add_class_cast_node",
        lambda graph, x, y: bridge.add_class_cast_node(
            graph, target_class_name, x, y),
        require_label="AddClassCastNode",
        pre_warnings=_w_guard,
        suggestion="target_class_name 需可解析（/Script/Module.Class）")


# ── L2-07 · add_macro_node（AddMacroNode）──

def _cmd_add_macro_node(bp_path: str, graph_name: str = "EventGraph",
                        macro_name: str = "", node_pos=None) -> dict:
    """L2-07 标准宏实例节点（AddMacroNode → UK2Node_MacroInstance）。

    C++（cpp:2270）：宏名须命中引擎标准宏库
      （/Engine/EditorBlueprintResources/StandardMacros.StandardMacros）或任一
      已加载宏图，否则 nullptr。
    可用宏：ForEachLoop / ForEachLoopWithBreak / WhileLoop / IsValid / Gate /
      FlipFlop ...。
    ⚠️ E5 纠错：DoOnce / MultiGate / Sequence 在 UE5.1 是**独立 K2 节点类**
      （非宏）→ 须走 create_node_by_class。
    """
    _pywrap_require_str("macro_name", macro_name)
    return _pywrap_make_node(
        bp_path, graph_name, node_pos, "add_macro_node",
        lambda graph, x, y: bridge.add_macro_node(graph, macro_name, x, y),
        require_label="AddMacroNode",
        suggestion="宏名示例 ForEachLoop / ForEachLoopWithBreak / WhileLoop / "
                   "IsValid / Gate / FlipFlop；DoOnce/MultiGate/Sequence 非宏 → "
                   "改用 create_node_by_class")


# ── L2-08 · add_switch_node（AddSwitchNode）──

def _cmd_add_switch_node(bp_path: str, graph_name: str = "EventGraph",
                         type_kind: str = "", enum_or_type_name: str = "",
                         node_pos=None) -> dict:
    """L2-08 Switch 节点（AddSwitchNode → UK2Node_SwitchInt/String/Name/Enum）。

    C++（cpp:2509）：TypeKind 空 → nullptr；未知 TypeKind（expect
      int|string|name|enum）→ nullptr；enum 时 EnumOrTypeName 为空或枚举不可解析
      → nullptr。
    """
    _pywrap_require_str("type_kind", type_kind)
    kind = type_kind.strip().lower()
    if kind not in _SWITCH_TYPE_KINDS:
        raise BridgeError(
            f"add_switch_node: type_kind 非法 {type_kind!r}（expect "
            f"{'|'.join(_SWITCH_TYPE_KINDS)}）",
            category="param_type")
    enum_name = str(enum_or_type_name or "")
    if kind == "enum" and not enum_name.strip():
        raise BridgeError(
            "add_switch_node: type_kind=enum 时 enum_or_type_name 必填"
            "（枚举全路径或短名）",
            category="param_type")
    # D-1 加固：enum 路径先解析（不可解析 → 不调引擎）
    _w_guard = []
    if kind == "enum":
        enum_name, _w_guard = _pywrap_guard_engine_class_path(
            enum_name, "enum_or_type_name", "param_type",
            kind="enum", label="add_switch_node")
    return _pywrap_make_node(
        bp_path, graph_name, node_pos, "add_switch_node",
        lambda graph, x, y: bridge.add_switch_node(graph, kind, enum_name,
                                                   x, y),
        require_label="AddSwitchNode",
        pre_warnings=_w_guard,
        suggestion="type_kind ∈ {int,string,name,enum}；enum 时 enum_or_type_name "
                   "需可解析为 UEnum")


# ── L2-09 · add_struct_node（AddStructNode）──

def _cmd_add_struct_node(bp_path: str, graph_name: str = "EventGraph",
                         struct_name: str = "", b_is_break: bool = False,
                         node_pos=None) -> dict:
    """L2-09 结构体 Make/Break 节点（AddStructNode → UK2Node_StructOperation）。

    C++（cpp:2634）：StructName 空 / 结构体不可解析 → nullptr。
    b_is_break=True → Break（拆解）；False → Make（组装）。
    """
    _pywrap_require_str("struct_name", struct_name)
    brk = _pywrap_bool("b_is_break", b_is_break)
    struct_name, _w_guard = _pywrap_guard_engine_class_path(
        struct_name, "struct_name", "param_type",
        kind="struct", label="add_struct_node")
    return _pywrap_make_node(
        bp_path, graph_name, node_pos, "add_struct_node",
        lambda graph, x, y: bridge.add_struct_node(graph, struct_name, brk,
                                                   x, y),
        require_label="AddStructNode",
        pre_warnings=_w_guard,
        suggestion="struct_name 示例 Vector / Rotator / Transform / HitResult")


# ── L2-10 · add_enum_literal_node（AddEnumLiteralNode）──

def _cmd_add_enum_literal_node(bp_path: str, graph_name: str = "EventGraph",
                               enum_name: str = "", value_name: str = "",
                               node_pos=None) -> dict:
    """L2-10 枚举字面量节点（AddEnumLiteralNode → UK2Node_EnumLiteral）。

    C++（cpp:3784）：Graph null / EnumName 空 / 枚举不可解析 → nullptr；
    ValueName 可选（非空时写入 Enum 引脚默认值；引脚找不到 → nullptr）。
    """
    _pywrap_require_str("enum_name", enum_name)
    val = str(value_name or "")
    enum_name, _w_guard = _pywrap_guard_engine_class_path(
        enum_name, "enum_name", "param_type",
        kind="enum", label="add_enum_literal_node")
    return _pywrap_make_node(
        bp_path, graph_name, node_pos, "add_enum_literal_node",
        lambda graph, x, y: bridge.add_enum_literal_node(graph, enum_name,
                                                         val, x, y),
        require_label="AddEnumLiteralNode",
        pre_warnings=_w_guard,
        suggestion="enum_name 需可解析（全路径 /Script/Module.EEnum 或短名）；"
                   "value_name 可选（如 NewEnumerator0）")


# ── L2-11 · add_composite_node（AddCompositeNode）──

def _cmd_add_composite_node(bp_path: str, graph_name: str = "EventGraph",
                            node_pos=None) -> dict:
    """L2-11 折叠图 Composite（AddCompositeNode → UK2Node_Composite）。

    C++（cpp:3649）：无业务入参（Graph + 位置）；节点类不符 / BoundGraph 未由
      PostPlacedNewNode 内建 → nullptr。
    """
    return _pywrap_make_node(
        bp_path, graph_name, node_pos, "add_composite_node",
        lambda graph, x, y: bridge.add_composite_node(graph, x, y),
        require_label="AddCompositeNode",
        suggestion="Composite 依赖 PostPlacedNewNode 内建 BoundGraph + Tunnel "
                   "进出节点；失败请核对 DLL 是否为 PATCH-A/B 版本")

# ── L2-12 · add_math_expression_node（AddMathExpressionNode）──

def _cmd_add_math_expression_node(bp_path: str, graph_name: str = "EventGraph",
                                  expression: str = "", node_pos=None) -> dict:
    """L2-12 数学表达式节点（AddMathExpressionNode → UK2Node_MathExpression）。

    C++（cpp:3682）：Graph null / Expression 空 / 节点类不符 → nullptr。
    expression 示例 "(A+B)*C"；节点创建后 ReconstructNode 触发 RebuildExpression
    按表达式自动生成入参引脚。
    """
    _pywrap_require_str("expression", expression)
    return _pywrap_make_node(
        bp_path, graph_name, node_pos, "add_math_expression_node",
        lambda graph, x, y: bridge.add_math_expression_node(graph, expression,
                                                            x, y),
        require_label="AddMathExpressionNode",
        suggestion='expression 示例 "(A+B)*C"（字母为入参变量名）')


# ── L2-13 · add_async_action_node（AddAsyncActionNode）──

def _cmd_add_async_action_node(bp_path: str, graph_name: str = "EventGraph",
                               factory_function_name: str = "",
                               factory_class_name: str = "",
                               activate_function_name: str = "",
                               node_pos=None) -> dict:
    """L2-13 异步任务节点（AddAsyncActionNode → UK2Node_AsyncAction）。

    C++（cpp:3717）：Graph null / FactoryFunctionName 空 → nullptr；
      FactoryClassName 不可解析 → nullptr；反射写 ProxyFactoryFunctionName /
      ProxyFactoryClass 失败 → nullptr。ActivateFunctionName 可空。
    """
    _pywrap_require_str("factory_function_name", factory_function_name)
    _pywrap_require_str("factory_class_name", factory_class_name)
    activate = str(activate_function_name or "")
    factory_class_name, _w_guard = _pywrap_guard_engine_class_path(
        factory_class_name, "factory_class_name", "param_type",
        kind="class", label="add_async_action_node")
    return _pywrap_make_node(
        bp_path, graph_name, node_pos, "add_async_action_node",
        lambda graph, x, y: bridge.add_async_action_node(
            graph, factory_function_name, factory_class_name, activate, x, y),
        require_label="AddAsyncActionNode",
        pre_warnings=_w_guard,
        suggestion="factory_class_name 为宿主类全路径（如 "
                   "/Script/Engine.KismetSystemLibrary）；activate_function_name "
                   "可空（代理 go 函数名）")


# ── L2-14 · add_create_delegate_node（AddCreateDelegateNode）──

def _cmd_add_create_delegate_node(bp_path: str, graph_name: str = "EventGraph",
                                  function_name: str = "", node_pos=None) -> dict:
    """L2-14 CreateDelegate / Bind Event（AddCreateDelegateNode）。

    C++（cpp:3519）：Graph null / FunctionName 空 / 节点类不符 → nullptr。
    节点创建后调用 SetFunction(FName) 绑定委托目标。
    """
    _pywrap_require_str("function_name", function_name)
    return _pywrap_make_node(
        bp_path, graph_name, node_pos, "add_create_delegate_node",
        lambda graph, x, y: bridge.add_create_delegate_node(graph,
                                                            function_name, x, y),
        require_label="AddCreateDelegateNode",
        suggestion="function_name 为委托绑定的函数/事件名（须存在于蓝图）")


# ── L2-15 · add_actor_bound_event_node（AddActorBoundEventNode）──

def _cmd_add_actor_bound_event_node(bp_path: str, graph_name: str = "EventGraph",
                                    actor_name: str = "",
                                    delegate_name: str = "",
                                    node_pos=None) -> dict:
    """L2-15 绑定关卡 Actor 实例事件（AddActorBoundEventNode）。

    C++（cpp:3447）：ActorName / DelegateName 空 → nullptr；关卡中找不到该 Actor
      或 Actor 类上无该委托 → nullptr。
    ⚠️ 与 add_component_bound_event_node 语义不同：本接口绑**关卡 Actor 实例**，
      后者绑 **SCS 组件委托**。
    """
    _pywrap_require_str("actor_name", actor_name)
    _pywrap_require_str("delegate_name", delegate_name)
    return _pywrap_make_node(
        bp_path, graph_name, node_pos, "add_actor_bound_event_node",
        lambda graph, x, y: bridge.add_actor_bound_event_node(
            graph, actor_name, delegate_name, x, y),
        require_label="AddActorBoundEventNode",
        suggestion="actor_name 须为当前关卡已放置 Actor 的**对象名**；"
                   "delegate_name 须存在于该 Actor 类（如 OnClicked / "
                   "OnComponentHit）")


# ── L2-16 · add_input_axis_event_node（AddInputAxisEventNode）──

def _cmd_add_input_axis_event_node(bp_path: str, graph_name: str = "EventGraph",
                                   axis_name: str = "",
                                   b_is_key_axis: bool = False,
                                   node_pos=None) -> dict:
    """L2-16 轴输入事件（AddInputAxisEventNode）。

    C++（cpp:3385）：Graph null / AxisName 空 → nullptr；
      bIsKeyAxis=true → UK2Node_InputAxisKeyEvent(FKey) —— AxisName 须为已注册
      按键名，否则 nullptr；
      bIsKeyAxis=false → UK2Node_InputAxisEvent(FName) —— Legacy 轴映射名。
    """
    _pywrap_require_str("axis_name", axis_name)
    is_key = _pywrap_bool("b_is_key_axis", b_is_key_axis)
    return _pywrap_make_node(
        bp_path, graph_name, node_pos, "add_input_axis_event_node",
        lambda graph, x, y: bridge.add_input_axis_event_node(graph, axis_name,
                                                             is_key, x, y),
        require_label="AddInputAxisEventNode",
        suggestion="b_is_key_axis=true 时 axis_name 须为已注册按键名（如 W）；"
                   "false 时为 Legacy 轴映射名（如 MoveForward）")


# ── L2-17 · add_input_action_event_node（AddInputActionEventNode）──

def _cmd_add_input_action_event_node(bp_path: str, graph_name: str = "EventGraph",
                                     action_name: str = "", node_pos=None) -> dict:
    """L2-17 InputAction 事件节点（AddInputActionEventNode）。

    C++（cpp:2373）：Graph null / ActionName 空 / 节点类不符 → nullptr。
    Legacy Input Action；Enhanced Input 走本接口需在线实测，不可用时降级
    add_input_key_event_node（B4）。
    """
    _pywrap_require_str("action_name", action_name)
    return _pywrap_make_node(
        bp_path, graph_name, node_pos, "add_input_action_event_node",
        lambda graph, x, y: bridge.add_input_action_event_node(graph,
                                                               action_name, x, y),
        require_label="AddInputActionEventNode",
        suggestion="Legacy Input Action 名（Project Settings → Input）；"
                   "Enhanced Input 请先在线实测，不支持时降级 "
                   "add_input_key_event_node")


# ── L2-18 · add_component_bound_event_node（AddComponentBoundEventNode）──

def _cmd_add_component_bound_event_node(bp_path: str,
                                        graph_name: str = "EventGraph",
                                        component_name: str = "",
                                        delegate_name: str = "",
                                        node_pos=None) -> dict:
    """L2-18 组件委托绑定事件节点（AddComponentBoundEventNode）。

    C++（cpp:2415）：ComponentName/DelegateName 非空 → 需 ① 图有属主蓝图
      ② 蓝图有 GeneratedClass ③ 组件属性存在于该 GeneratedClass（即组件已加进
      SCS 且已编译）④ 组件类上存在该委托（InitializeComponentBoundEventParams）
      → 任一不满足即 nullptr。
    D-1 关联（SCS 外链）：组件须先 add_component 并 compile_blueprint；否则
      GeneratedClass 上无组件属性。本接口一律 fail-loud，不把引擎断言
      （check(Blueprint) 崩溃级）留给调用方。
    """
    _pywrap_require_str("component_name", component_name)
    _pywrap_require_str("delegate_name", delegate_name)
    return _pywrap_make_node(
        bp_path, graph_name, node_pos, "add_component_bound_event_node",
        lambda graph, x, y: bridge.add_component_bound_event_node(
            graph, component_name, delegate_name, x, y),
        require_label="AddComponentBoundEventNode",
        suggestion="先 add_component 添加组件 → compile_blueprint 编译（GeneratedClass "
                   "上须出现该组件属性）→ 再绑委托；若组件属性找不到，检查 SCS 外链"
                   "是否完好（D-1 家族根因），本接口已 fail-loud 兜底不带崩编辑器")

# ── L2-19 · remove_component（RemoveComponent · 裁决 A 语义）──

def _cmd_remove_component(bp_path: str, comp_name: str = "") -> dict:
    """L2-19 移除 SCS 组件（RemoveComponent · 裁决 A 语义）。

    C++（cpp:2769）：`bool RemoveComponent(UBlueprint*, FString ComponentName,
      FString& OutError)`。失败透传文本用 `return true + OutError 非空`（绕过
      UE 反射吞 OutError），故 Python 侧：'' = 成功 / None = 失败（Blueprint
      为 null，文本被吞）/ 非空 str = 失败 + 错误文本。
    回读校验（R1 降级）：component_templates 可读且组件仍在 → fail-loud。
    D-1 关联：SCS 外链断裂时组件不在 SCS → C++ 返回 "not found" → 本封装
      fail-loud 并给出 SCS 排查建议。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("comp_name", comp_name)
    bp = _resolve_blueprint(bp_path)
    before, berr = _pywrap_read_component_names(bp)
    if before is not None and comp_name not in before:
        raise BridgeError(
            f"remove_component: 蓝图 {bp_path} 组件清单中无 '{comp_name}'，"
            f"实读 {before}",
            category="component_remove",
            suggestion="组件名须与 add_component 时的 comp_name 一致（SCS "
                       "VariableName）；SCS 外链断裂（D-1 家族）会致组件不在 SCS 中")
    result = _safe_call(bridge.remove_component, bp, comp_name,
                        error_cat="component_remove")
    ok, _txt, err = _pywrap_out_str_result(result, "RemoveComponent",
                                           f"component={comp_name}")
    if not ok:
        raise BridgeError(
            err, category="component_remove",
            suggestion="确认组件名与 SCS VariableName 一致；SCS 外链断裂（D-1 家族）"
                       "会致组件不在 SCS 中 → 需先在编辑器内重挂 SCS / 重建蓝图")
    after, aerr = _pywrap_read_component_names(bp)
    warnings = []
    if berr:
        warnings.append(f"组件清单回读不可用（{berr}）")
    if after is None:
        warnings.append(f"组件移除回读不可用（{aerr or berr}）")
        removed = None
    else:
        removed = comp_name not in after
        if not removed:
            raise BridgeError(
                f"组件移除后仍在回读清单中: '{comp_name}'，实读 {after}",
                category="component_remove")
    bp = None
    return {"ok": True, "comp_name": comp_name, "removed": removed,
            "components": after, "warnings": warnings}


# ── L2-20 · add_implemented_interface（AddImplementedInterface）──

def _cmd_add_implemented_interface(bp_path: str,
                                   interface_class_path: str = "") -> dict:
    """L2-20 蓝图实现新接口（AddImplementedInterface）→ bool。

    C++（cpp:2827）：Blueprint null / InterfaceClassPath 空 /
      FBlueprintEditorUtils::ImplementNewInterface 失败 → false → fail-loud。

    🔴 D-1 加固（T-20260912-UE5BRIDGE-PYWRAP-L2-GUARD）：C++ 侧**不解析**
      InterfaceClassPath，直接把字符串以 FName 交给
      `FBlueprintEditorUtils::ImplementNewInterface`（cpp:2841-2843）→ 引擎内部
      `check(InterfaceClass)`（BlueprintEditorUtils.cpp:6463）断言 →
      **UnrealEditor 硬崩**（2026-09-12 19:1x 实测 PID 68416）。∴ 本封装在
      **调用引擎前**先解析（`_pywrap_guard_engine_class_path`，kind=interface）：
      全路径解析不到 → `BridgeError` fail-loud，**绝不把不可解析路径递给引擎**。
    回读校验（R1 降级）：generated_class().implemented_interfaces 可读时做
      短名比对；未命中 → 告警（非阻断，因该属性在非 GameThread 下可能滞后）。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("interface_class_path", interface_class_path)
    interface_class_path, _w_guard = _pywrap_guard_engine_class_path(
        interface_class_path, "interface_class_path", "interface_add",
        kind="interface", label="add_implemented_interface")
    bp = _resolve_blueprint(bp_path)
    ret = _safe_call(bridge.add_implemented_interface, bp,
                     interface_class_path, error_cat="interface_add")
    if ret is None or ret is False:
        raise BridgeError(
            f"AddImplementedInterface 返回 {ret!r}（失败）: "
            f"{interface_class_path}",
            category="interface_add",
            suggestion="interface_class_path 需为可加载接口类路径"
                       "（/Script/Module.UInterface 或 /Game/.../BPI_X.BPI_X_C），"
                       "且蓝图尚未实现该接口")
    names, nerr = _pywrap_read_implemented_interfaces(bp)
    short = _pywrap_short_class_name(interface_class_path)
    warnings = list(_w_guard)
    if names is None:
        warnings.append(f"接口清单回读不可用（{nerr}）")
        readback = None
    else:
        readback = (short in names) or (interface_class_path in names)
        if not readback:
            warnings.append(
                f"接口清单回读未命中 {short!r}，实读 {names}"
                "（回读通道可用但未命中 → 告警不阻断，R1 降级）")
    bp = None
    return {"ok": True, "interface_class_path": interface_class_path,
            "readback": readback, "interfaces": names, "warnings": warnings}


# ── L2-21 · add_event_dispatcher（AddEventDispatcher）──

def _cmd_add_event_dispatcher(bp_path: str, delegate_name: str = "") -> dict:
    """L2-21 事件分发器（AddEventDispatcher）。

    C++（cpp:3307）：`bool AddEventDispatcher(UBlueprint*, FString DelegateName,
      FString& OutError)`；**所有失败分支均 `return false`** → UE Python 反射下
      OutError 被吞 → Python 只拿到 None（错误文本仅在 UE 日志）。因此：
      '' = 成功 / None = 失败（详情见 UE 日志）。
    实现路径（引擎无公开静态 API）：AddMemberVariable(PC_MCDelegate) +
      CreateNewGraph(DelegateSignatureGraphs) + K2Schema 默认节点/终止符。
    重名语义（契约 ⑤）：同名分发器已存在 → C++ 拒绝（"already exists"）→
      Python 侧 fail-loud，禁静默双建。
    """
    _pywrap_require_bridge()
    _pywrap_require_str("bp_path", bp_path)
    _pywrap_require_str("delegate_name", delegate_name)
    bp = _resolve_blueprint(bp_path)
    result = _safe_call(bridge.add_event_dispatcher, bp, delegate_name,
                        error_cat="delegate_add")
    ok, _txt, err = _pywrap_out_str_result(result, "AddEventDispatcher",
                                           f"delegate={delegate_name}")
    if not ok:
        raise BridgeError(
            err + "（可能：同名分发器已存在 / AddMemberVariable 或 CreateNewGraph "
                  "失败，详见 UE 日志）",
            category="delegate_add",
            suggestion="确认蓝图未含同名事件分发器；失败详情见 UE 日志 "
                       "[BPBridge] AddEventDispatcher")
    graphs, gerr = _pywrap_read_delegate_graphs(bp)
    warnings = []
    if graphs is None:
        warnings.append(f"事件分发器回读不可用（{gerr}）")
        readback = None
    else:
        readback = delegate_name in graphs
        if not readback:
            warnings.append(
                f"事件分发器回读未命中 {delegate_name!r}，实读 {graphs}")
    bp = None
    return {"ok": True, "delegate_name": delegate_name, "readback": readback,
            "delegate_graphs": graphs, "warnings": warnings}


# ── 落盘命令 · save_asset（T-20260913-UE5BRIDGE-SAVE-ASSET · 47→48 条）──

def _cmd_save_asset(asset_path: str, only_if_is_dirty: bool = False) -> dict:
    """落盘单个资产（unreal.EditorAssetLibrary.save_asset）——白名单化落盘命令。

    T-20260913-UE5BRIDGE-SAVE-ASSET 新增：
      背景：改蓝图（如 clamp Min 0.0→−800.0）后**落盘只能绕 `ue_send.py`**
      （任意代码执行逃生门），与 A-003/A-004「白名单化」设计相悖。本命令把
      「保存单个资产」这一最小落盘能力白名单化，走 GameThread 原子执行。

    🔴 KB 语义搜索结果（编码前 · kb=agent_knowledge）：
      1. 「read_variables 假阳性修复模式」[已验证 2026-08-20]：吞异常 → 返回
         `success=true` 的假阳性结构是最难排查的失败模式之一。正确姿势 =
         数据层抛 BridgeError(category/suggestion)、命令层 success=false +
         error 显式字段。→ 本命令**fail-loud**：save_asset 返回 False /
         资产不存在 → 必抛 BridgeError(category="save_failure")，绝不返回
         `"success": true` 的假阳性。
      2. 「离线已实施≠进程内已生效」[已验证 2026-09-12]：改完磁盘须热载才能在
         进程内生效 → 联调时先热载（保 token 头）再验。
      3. 白名单 + handler **必须双侧同步**：漏一侧 → Unknown command。

    Args:
        asset_path: UE 包路径（必须以 '/' 开头），如
            "/Game/Blueprints/BP_ArrowUpButton"。
        only_if_is_dirty: True → 仅当资产有未保存改动时才落盘（默认 False）。

    Returns:
        {"ok": True, "asset_path": ..., "saved": True, "is_dirty_before": bool|None}
        （is_dirty_before 用 hasattr 守卫，UE5.1 可选；不可读时为 None，不阻断）
    """
    _pywrap_require_str("asset_path", asset_path)
    if not asset_path.startswith("/"):
        raise BridgeError(
            "asset_path 必须以 '/' 开头（收到 %r）" % (asset_path,),
            category="param_type",
            suggestion="传 UE 包路径，例如 /Game/Blueprints/BP_ArrowUpButton",
        )
    if not _safe_game_pkg(asset_path):
        raise BridgeError("save_asset 只允许规范 /Game/ 路径（收到 %r）"
                          % asset_path, category="param_type")
    only_dirty = _pywrap_bool("only_if_is_dirty", only_if_is_dirty)

    def _do():
        eal = getattr(unreal, "EditorAssetLibrary", None)
        if eal is None:
            raise BridgeError(
                "unreal.EditorAssetLibrary 不可用（编辑器库未加载）",
                category="save_failure",
            )
        # 存在性守卫（fail-loud，使负路径确定性失败而非依赖引擎返回值）
        if hasattr(eal, "does_asset_exist") and not eal.does_asset_exist(asset_path):
            raise BridgeError(
                "资产不存在: %s" % asset_path,
                category="save_failure",
                suggestion="确认包路径正确（如 /Game/Blueprints/BP_ArrowUpButton）",
            )
        # is_dirty 读取（UE5.1 可选：hasattr 守卫，不可读 → None，不阻断）
        is_dirty_before = None
        if hasattr(eal, "is_dirty"):
            try:
                is_dirty_before = bool(eal.is_dirty(asset_path))
            except Exception:
                is_dirty_before = None
        try:
            saved = eal.save_asset(asset_path, only_if_is_dirty=only_dirty)
        except TypeError:
            saved = eal.save_asset(asset_path)  # UE 版本无关键字参数 → 退化
        if not saved:
            raise BridgeError(
                "EditorAssetLibrary.save_asset 返回 False: %s" % asset_path,
                category="save_failure",
                suggestion="确认资产存在且磁盘可写；失败详情见 UE 日志",
            )
        return {"ok": True, "asset_path": asset_path, "saved": True,
                "is_dirty_before": is_dirty_before}

    return _run_on_game_thread_sync(_do, timeout=30.0)


# ══════════════════════════════════════════════════════════
#  资产级联删除（D-1 · T-20260913-UE5BRIDGE-DELETE-COMPILE-FIX）
# ══════════════════════════════════════════════════════════
#
# 🔴 编码前 KB 语义搜索（kb=agent_knowledge）：
#   1. 「BP_APIGAP_TMP 残留清理闭环」[已验证 2026-08-20] —— 标准三件套 =
#      ① delete_asset ② 保存（**D-7 起作用域收窄**：只保存「相关包」= 引用者包；
#      旧「全工程脏包」写法 = save_dirty_packages 双 True，2026-09-17 已废除）
#      ③ 复读验证。
#   2. 「read_variables 假阳性修复模式」[已验证 2026-08-20] —— 吞异常/吞返回值
#      → 假 success 是最难排查的失败模式 → 本实现 fail-loud。
#   3. 历史同型（tasks_state 实录）：L4468 `delete_asset` 6 轮全失败(0/3) + 双崩溃；
#      L4328 只落规范「返回 False 必须 fail-fast 硬中止」，**根因未修**
#      → 本次把该规范**落成代码**。
#
# 🔴 事故背景（2026-09-13 T-20260913-UE5-DELIVERY-E2E 三柱 ③ FAIL）：
#    BP_P8e 经 3 轮 full-GC + delete_asset 仍 `does_asset_exist=True`
#    （磁盘 13,185 B 在位）→ 旧代码 L6276 **丢弃返回值、静默吞掉**。
#
# 🔴 UE5.1.1 **在线实测** API 面（2026-09-13 · PID 77252 · 脚本 p_intro.py）：
#    EditorAssetLibrary.delete_asset ✅ / delete_loaded_asset ✅ / does_asset_exist ✅
#    EditorAssetLibrary.load_asset ✅ / save_all_packages ❌ / is_dirty ❌
#    EditorLoadingAndSavingUtils.save_dirty_packages ✅（API 存在）
#      ⚠️ D-7（T-20260917-D7-DELETE-SCOPE-FIX）：删除路径**不再调用**该全局 API
#         —— 作用域过宽（全工程脏包）→ 会静默提交用户未保存的关卡/资产；
#         改用 EditorAssetLibrary.save_asset(path, only_if_is_dirty=True)
#         逐「相关包」（引用者包）保存。
#    SystemLibrary.collect_garbage ✅
#    AssetEditorSubsystem.close_all_editors_for_asset ✅
#    → 全部通过 hasattr 守卫调用（缺 API 时优雅跳过，不抛）
_DELETE_MAX_ATTEMPTS_HARD = 3


def _delete_asset_cascade(asset_path: str, max_attempts: int = 3) -> dict:
    """级联删除资产 + 删后回读验证 + 重试上限 → 失败 fail-loud（GameThread 执行）。

    步骤：① 若已在编辑器打开 → 关其 asset editor（用完**立即丢弃** wrapper 引用 + GC）
          ② 删后成功才保存（**D-7 作用域收窄**）：只保存「相关包」= 删除前经
             AssetRegistry 只读查得的**引用者包**；⛔ 绝不做全工程脏包保存；
             无从定位相关包 → 跳过并记 `save_skipped_reason`（诚实记录）
          ③ 主删 **只传路径** delete_asset(path)（F1 · 删除帧不持 Python 包装器）
          ④ SystemLibrary.collect_garbage()（每轮删除前 + 删除后各一次）
          ⑤ 回读 does_asset_exist
          ⑦ 仍存在 → fail-loud（F4：带 engine_reason / referencers 结构化诊断）

    🔴 F1（T-20260913-UE5BRIDGE-DELETE-F1F4-FIX · 根因 H1 高置信）：
       旧代码 `_obj = eal.load_asset(pkg_path)` 与 `delete_loaded_asset(_obj)` 同帧
       → Python 侧保留 `unreal.Object` 包装器 → 引擎视为被 root 对象
       `GCObjectReferencer_0` **外部引用** → `ObjectTools::DeleteSingleObject`
       `return false`（日志 `External referencers of …`）。改法 = 删除调用**只传
       路径**，同帧内不残留任何 `unreal.Object` 引用，每轮删除前 `collect_garbage()`。

    Returns:
        {"success": True, "deleted": True, "attempts": N, "trail": [...],
         "save_scope": "related_packages", "saved_related_packages": [...],
         "save_skipped_reason": <str，仅当本次未做任何保存时出现>}
        {"success": False, "deleted": False, "attempts": N,
         "error": "asset still exists after delete: <path>",
         "engine_reason": <str>, "referencers": <list|None>,
         "attempted_with_python_hold": False, "trail": [...]}
        ⛔ 绝不返回假成功（删后回读仍存在 → success=False）
    """
    _pywrap_require_str("asset_path", asset_path)
    if not asset_path.startswith("/"):
        raise BridgeError(
            "asset_path 必须以 '/' 开头（收到 %r）" % (asset_path,),
            category="param_type",
            suggestion="传 UE 包路径，例如 /Game/Blueprints/BP_Tmp",
        )
    try:
        max_attempts = int(max_attempts)
    except (TypeError, ValueError):
        max_attempts = _DELETE_MAX_ATTEMPTS_HARD
    max_attempts = max(1, min(_DELETE_MAX_ATTEMPTS_HARD, max_attempts))
    pkg_path = asset_path.split(".")[0]
    if not _safe_game_pkg(pkg_path):
        raise BridgeError("delete_asset 只允许 /Game/ 项目内容路径（收到 %r）"
                          % asset_path, category="param_type")

    def _do():
        _u = globals().get("unreal")
        eal = getattr(_u, "EditorAssetLibrary", None) if _u else None
        if eal is None:
            raise BridgeError("unreal.EditorAssetLibrary 不可用（编辑器库未加载）",
                              category="delete_failure")
        trail = []

        def _exists():
            """删后回读；探测异常 → 保守视为仍存在（fail-loud，不谎报成功）。"""
            try:
                if hasattr(eal, "does_asset_exist"):
                    return (bool(eal.does_asset_exist(pkg_path)) or
                            bool(eal.does_asset_exist(asset_path)))
            except Exception as _e:
                trail.append("readback_err: %s" % _e)
                return True
            return True

        # ── D-7（T-20260917-D7-DELETE-SCOPE-FIX）：删后保存**作用域收窄** ──────
        # 🔴 缺陷：旧实现 `_save_dirty()` 调 save_dirty_packages 双 True =
        #    **全工程脏包保存** → 用户「删一个临时资产」会**静默提交**所有未保存的
        #    关卡 / 资产（无提示、无参数可关）。实测（2026-09-17 13:00:08）：一次
        #    临时资产清理连带写了 /Game/Maps/ElevatorDemo.umap 及 BP_HelloMirror /
        #    BP_E2E_Test / BP_CPPOnline_Comp 共 4 件**既有**资产。
        # 🔴 修法：作用域 =「与被删资产相关」= **删除前**经 AssetRegistry 只读查得的
        #    磁盘**引用者包**；保存走**已白名单化验证**的
        #    `EditorAssetLibrary.save_asset(path, only_if_is_dirty=True)`
        #    （T-20260913-UE5BRIDGE-SAVE-ASSET ✅）—— 只传**路径字符串**，
        #    不产生 `unreal.Object` 包装器（不违反 F1）。
        # ⛔ 被删资产**自身**的包不保存：它已从磁盘/内存移除，保存它只可能把它
        #    **写回磁盘（复活）**；旧全局保存若命中它同样会复活 → 排除是严格更优。
        # ⛔ 无从定位相关包（registry 不可用 / 零引用者 / save_asset 缺失）→
        #    **不调用任何保存**，由 `save_skipped_reason` 显式记录
        #    （⛔ 不静默跳过、⛔ 不吞异常：每条失败都进 trail）。
        _related_pkgs = []
        _related_reason = None

        def _referencers_options():
            """构造 get_referencers 的 options（hasattr 守卫 · 失败 → None 退化）。"""
            _cls = getattr(_u, "AssetRegistryDependencyOptions", None) if _u else None
            if _cls is None:
                return None
            try:
                _o = _cls()
                for _f in ("include_hard_package_references",
                           "include_soft_package_references",
                           "include_hard_management_references",
                           "include_soft_management_references"):
                    if hasattr(_o, _f):
                        try:
                            setattr(_o, _f, True)
                        except Exception:                        # noqa: BLE001
                            pass
                return _o
            except Exception as _e:                              # noqa: BLE001
                trail.append("referencers_opts_err: %s" % _e)
                return None

        def _collect_related():
            """删除**前**采集「相关包」（只读 · hasattr 守卫 · 绝不抛）。

            必须在首次删除调用**之前**执行：删除成功后注册表项即消失，
            get_referencers 将无从定位 —— 即派单所述「确实无从定位相关包」场景。
            返回 None = 采集可用；返回 str = 跳过原因（写入 save_skipped_reason）。
            """
            _arh = getattr(_u, "AssetRegistryHelpers", None) if _u else None
            _get_ar = getattr(_arh, "get_asset_registry", None) if _arh else None
            if not callable(_get_ar):
                return "referencers_api_unavailable"
            try:
                _ar = _get_ar()
                _get_ref = (getattr(_ar, "get_referencers", None)
                            if _ar is not None else None)
                if not callable(_get_ref):
                    return "referencers_api_unavailable"
                _opts = _referencers_options()
                try:
                    _refs = (_get_ref(pkg_path, _opts) if _opts is not None
                             else _get_ref(pkg_path))
                except TypeError:
                    _refs = _get_ref(pkg_path)                   # 旧签名退化
                for _r in (_refs or []):
                    _n = str(_r).split(".")[0]
                    if _n and _n != pkg_path and _n not in _related_pkgs:
                        _related_pkgs.append(_n)
            except Exception as _e:                              # noqa: BLE001
                trail.append("related_query_err: %s" % _e)
                return "referencers_query_failed"
            if not _related_pkgs:
                return "no_related_packages"
            trail.append("related_packages: %r" % (_related_pkgs,))
            return None

        def _save_related():
            """删后保存**相关包**（只脏包）。返回 (saved_list, skipped_reason)。

            · 作用域 = _related_pkgs（引用者包）—— 已排除被删包自身
            · only_if_is_dirty=True → 非脏的相关包**不落盘**（不制造无谓写盘）
            · 任一异常/API 缺失 → 记 trail + 跳过原因，绝不抛、绝不静默
            """
            if _related_reason is not None:
                trail.append("save_skipped: %s" % _related_reason)
                return [], _related_reason
            _fn = getattr(eal, "save_asset", None)
            if not callable(_fn):
                trail.append("save_skipped: save_asset_api_unavailable")
                return [], "save_asset_api_unavailable"
            _saved = []
            for _p in _related_pkgs:
                try:
                    _ok = _fn(_p, only_if_is_dirty=True)
                except TypeError as _e:
                    # ⛔ 不退化到无参调用：那会落盘**非脏**包（= 又一种越权写盘）
                    trail.append("save_related_typeerr %s: %s" % (_p, _e))
                    continue
                except Exception as _e:                          # noqa: BLE001
                    trail.append("save_related_err %s: %s" % (_p, _e))
                    continue
                trail.append("save_asset(%s, only_if_is_dirty=True): %r"
                             % (_p, _ok))
                if _ok:
                    _saved.append(_p)
            if not _saved:
                trail.append("save_related: none_dirty")
                return [], "related_packages_not_dirty"
            return _saved, None

        def _gc():
            _slib = getattr(_u, "SystemLibrary", None) if _u else None
            _fn = getattr(_slib, "collect_garbage", None) if _slib else None
            if callable(_fn):
                try:
                    _fn()
                    return True
                except Exception as _e:
                    trail.append("collect_garbage_err: %s" % _e)
            return False

        # 已不存在 → 幂等成功（不谎报「删了」）
        if not _exists():
            return {"success": True, "asset_path": pkg_path, "deleted": True,
                    "attempts": 0, "already_absent": True, "trail": trail}

        # ① 关 asset editor
        # 🔴 v2 修正（2026-09-13 在线实测 · D-1 首轮 FAIL 根因）：
        #    `unreal.AssetEditorSubsystem.close_all_editors_for_asset(obj)` 是
        #    **未绑定方法** → TypeError:
        #      "descriptor 'close_all_editors_for_asset' requires a
        #       'AssetEditorSubsystem' object but received a 'Blueprint'"
        #    → 编辑器**永不关闭** → 资产被编辑器持有 → delete 恒返回 False。
        #    正确式：unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
        #            .close_all_editors_for_asset(asset)  → 返回关闭的编辑器数
        _aes = None
        try:
            _g = getattr(_u, "get_editor_subsystem", None) if _u else None
            if callable(_g):
                _aes = _g(getattr(_u, "AssetEditorSubsystem"))
        except Exception as _e:
            trail.append("get_editor_subsystem_err: %s" % _e)
        if _aes is not None and hasattr(_aes, "close_all_editors_for_asset"):
            # 🔴 F1：关编辑器需要对象，但**用完必须立即丢弃引用**——否则该
            #    `unreal.Object` 包装器会活到删除帧 → 引擎判「外部引用」→ 删除恒 False。
            _tmp = None
            try:
                _tmp = eal.load_asset(pkg_path)
                if _tmp is not None:
                    trail.append("close_all_editors_for_asset: %r"
                                 % (_aes.close_all_editors_for_asset(_tmp),))
            except Exception as _e:
                trail.append("close_editors_err: %s" % _e)
            finally:
                _tmp = None  # F1：丢弃包装器引用（下帧 GC 回收）
        _gc()  # F1：删除前强制回收，确保无残留 wrapper 引用进入删除帧

        # ② 删前**不**保存 dirty 包
        # 🔴 v2 修正（2026-09-13 在线实测）：删前 `save_dirty_packages` 会把
        #    「仅存在于内存/资产注册表」的新资产**落盘复活**
        #    （实测 BP_D1D2_Tmp1 删除前磁盘不存在 → 级联跑完出现 21,541 B）
        #    → 与删除目标相反。保存只在**删除成功后**执行
        #    （KB 三件套语义：delete → save → verify，用于防下次启动复活）。

        def _engine_diag():
            """F4：结构化收集「引擎拒绝删除」的只读诊断（hasattr 守卫 · 绝不抛）。

            · `referencers`：经 AssetRegistry 查（**只读**）→ 引用者包路径列表。
              `AssetRegistryHelpers.get_asset_registry()` 为本仓既有 API
              （见 scripts/analyzer.py L177）；`get_referencers` 经 hasattr 守卫，
              任一缺失/异常 → 优雅降级（记 `referencers_api=False` 或 `*_err`）。
            · `attempted_with_python_hold`：**自证式诊断** —— F1 后恒为 False
              （删除调用不再持有 Python 包装器）。
            · `reason`：引用者非空 → `engine_refused_external_referencer:<pkg,...>`；
              为空/不可查 → `asset_still_exists_after_delete`
              （引擎未报引用者 → 疑 H2/H3 类隐式持有，留待后续单）。
            """
            diag = {"attempted_with_python_hold": False}
            _arh = getattr(_u, "AssetRegistryHelpers", None) if _u else None
            _get_ar = getattr(_arh, "get_asset_registry", None) if _arh else None
            if callable(_get_ar):
                try:
                    _ar = _get_ar()
                    _get_ref = (getattr(_ar, "get_referencers", None)
                                if _ar is not None else None)
                    if callable(_get_ref):
                        diag["referencers_api"] = True
                        # UE5.1 **在线只读实测**签名（2026-09-13 · PID 79764）：
                        #   `get_referencers(package_name, reference_options) -> Array[Name]`
                        #   `reference_options` = `unreal.AssetRegistryDependencyOptions`
                        #   （默认 include_hard/soft_package_references=True）
                        #   ⚠️ 只含**磁盘**引用者（"On disk references ONLY"）。
                        #   ⛔ 不做未验证试探：先按已验证签名传 options，TypeError
                        #      才退化到单参（旧绑定）。
                        _opts = None
                        _opt_cls = getattr(_u, "AssetRegistryDependencyOptions", None)
                        if _opt_cls is not None:
                            try:
                                _opts = _opt_cls()
                                for _f in ("include_hard_package_references",
                                           "include_soft_package_references",
                                           "include_hard_management_references",
                                           "include_soft_management_references"):
                                    if hasattr(_opts, _f):
                                        try:
                                            setattr(_opts, _f, True)
                                        except Exception:            # noqa: BLE001
                                            pass
                            except Exception as _e:                  # noqa: BLE001
                                _opts = None
                                diag["referencers_opts_err"] = "%s: %s" % (
                                    type(_e).__name__, _e)
                        try:
                            _refs = (_get_ref(pkg_path, _opts) if _opts is not None
                                     else _get_ref(pkg_path))
                        except TypeError:
                            _refs = _get_ref(pkg_path)               # 旧签名退化
                        diag["referencers"] = ([str(_r) for _r in _refs]
                                               if _refs else [])
                    else:
                        diag["referencers_api"] = False
                except Exception as _e:
                    diag["referencers_api"] = True
                    diag["referencers_err"] = "%s: %s" % (type(_e).__name__, _e)
            else:
                diag["referencers_api"] = False
            _refs_out = diag.get("referencers")
            if _refs_out:
                diag["reason"] = ("engine_refused_external_referencer:"
                                  + ",".join(_refs_out))
            elif diag.get("referencers") is not None:
                # 磁盘零引用者却删不掉 → 非「外部引用」类；疑内存/子系统隐式持有
                diag["reason"] = "no_on_disk_referencer_but_undeletable"
            else:
                diag["reason"] = "asset_still_exists_after_delete"
            return diag

        # ── D-7：删除**前**采集「相关包」（删后注册表项消失 → 无从定位）──
        _related_reason = _collect_related()

        for _i in range(1, max_attempts + 1):
            # ③ 主删：**只传路径**（F1 · T-20260913-UE5BRIDGE-DELETE-F1F4-FIX）
            #    🔴 旧代码 `_obj = eal.load_asset(pkg_path)` + 同帧
            #       `delete_loaded_asset(_obj)` → 引擎判「外部引用」→ 恒 False。
            #    ✅ `delete_asset(path)` 内部 C++ 栈上 load→delete，临时对象随调用
            #       结束释放（不进 FGCObject / 不留 Python 引用）。
            _gc()  # F1：删除前强制 GC —— 保证「删除调用」与「Python 对象引用」不同帧
            _da = getattr(eal, "delete_asset", None)
            if callable(_da):
                try:
                    trail.append("attempt%d delete_asset(path): %r"
                                 % (_i, _da(pkg_path)))
                except Exception as _e:
                    trail.append("attempt%d delete_asset_err: %s" % (_i, _e))
            # ④ full GC（删除调用返回后再回收一次）
            if _gc():
                trail.append("attempt%d collect_garbage: ok" % _i)
            # ⑤ 回读
            if not _exists():
                _saved, _skip = _save_related()
                _out = {"success": True, "asset_path": pkg_path, "deleted": True,
                        "attempts": _i, "trail": trail,
                        "save_scope": "related_packages",
                        "saved_related_packages": _saved}
                if _skip:
                    _out["save_skipped_reason"] = _skip
                return _out

        # ⑦ fail-loud：重试上限内仍存在（F4：附结构化引擎诊断）
        _diag = _engine_diag()
        trail.append("engine_diag: %r" % (_diag,))
        return {"success": False, "asset_path": pkg_path, "deleted": False,
                "attempts": max_attempts,
                "error": "asset still exists after delete: %s" % pkg_path,
                "engine_reason": _diag.get("reason"),
                "referencers": _diag.get("referencers"),
                "attempted_with_python_hold": False,
                "trail": trail}

    return _run_on_game_thread_sync(_do, timeout=60.0)


def _snapshot_root(task_id: str) -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "ue5_bridge", "snapshots", task_id)


def _safe_game_pkg(asset_path: str) -> str:
    """规范化 /Game/ 包路径；含 .. / . / 反斜杠 → 返回 ""（防目录逃逸）。"""
    raw = str(asset_path or "").strip()
    if "\\" in raw or "/./" in raw or "/../" in raw or raw.startswith(("./", "../")):
        return ""
    pkg = raw.split(".")[0].split(":")[0].rstrip("/")
    if not pkg.startswith("/Game/"):
        return ""
    parts = pkg.lstrip("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        return ""
    return pkg


def _asset_pkg_and_rel(asset_path: str):
    """'/Game/X/Y.BP_Y' → ('/Game/X/Y', 'X/Y.uasset')；非 /Game/ → (原路径, None)。"""
    pkg = _safe_game_pkg(asset_path)
    if not pkg:
        return str(asset_path or ""), None
    return pkg, pkg[len("/Game/"):] + ".uasset"


def _snapshot_asset_file(asset_path: str, reason: str) -> dict:
    """删除前把 .uasset 复制到任务快照目录（无任务/文件不存在 → 空 dict）。

    失败不阻断主操作（快照是保险，不是前置条件）；返回
    {asset, snapshot, bytes, reason}。
    """
    if not (_TASK_JOURNAL_AVAILABLE and _task_journal is not None):
        return {}
    task_id = _task_journal.active_task_id()
    if not task_id:
        return {}
    pkg, rel = _asset_pkg_and_rel(asset_path)
    if not rel:
        return {}

    def _do():
        content = unreal.Paths.convert_relative_path_to_full(
            unreal.Paths.project_content_dir())
        src = os.path.join(str(content), rel.replace("/", os.sep))
        if not os.path.isfile(src):
            return {}
        root = _snapshot_root(task_id)
        os.makedirs(root, exist_ok=True)
        safe = rel.replace("/", "__").replace("\\", "__")
        dst = os.path.join(root, safe)
        shutil.copy2(src, dst)
        return {"asset": pkg, "snapshot": dst, "bytes": os.path.getsize(dst),
                "reason": reason}

    try:
        return _gt_run_handler(_do, timeout=30.0) or {}
    except Exception as e:
        _OP_LOG.warning("asset snapshot failed (%s): %s", asset_path, e)
        return {}


def _cmd_delete_asset(asset_path: str, max_attempts: int = 3) -> dict:
    """白名单命令：级联删除单个资产（fail-loud）。

    T-20260913-UE5BRIDGE-DELETE-COMPILE-FIX · D-1
      · 删除路径唯一入口（`/command delete_asset` 路由）
      · 重试上限 ≤3（硬上限 `_DELETE_MAX_ATTEMPTS_HARD`），超限即 fail-loud
      · 返回体带 `_fail_loud=True` → `/command` 层据此把 success 置 false
      · D-7（T-20260917-D7-DELETE-SCOPE-FIX）：删后保存**只作用于相关包**
        （引用者包）→ 返回体带 `save_scope` / `saved_related_packages` /
        `save_skipped_reason`；⛔ 不再做全工程脏包保存（旧行为会静默提交
        用户未保存的关卡/资产）。
    """
    # v5.0：删除前快照（任务级回滚用）；删除成功才记 deleted 账
    snap = _snapshot_asset_file(asset_path, "deleted")
    out = _delete_asset_cascade(asset_path, max_attempts)
    if out.get("success") and _TASK_JOURNAL_AVAILABLE and _task_journal is not None:
        try:
            pkg, _ = _asset_pkg_and_rel(asset_path)
            _task_journal.note_deleted_asset(pkg, snap)
        except Exception:
            pass
    out["_fail_loud"] = True
    return out


def _cmd_unpin_console_refs(asset_path: str = "") -> dict:
    """修法3（T-20260914-UE5BRIDGE-BUILD-HOLD-FIX）：会话内「解绑」被污染的
    PyConsoleGlobalDict → 免重启 UE 的现场恢复 + 根因判别工具。

    原理：GT 内 exec 时 `globals()` **正是** PyConsoleGlobalDict（引擎
    PythonScriptPlugin.cpp L1416-1422）→ 删除「path 指向目标资产」的
    UObject 包装器 → `collect_garbage()`。脚本本体在
    `unpin_console_refs.py.tmpl`（同 /build 的 N01 模板约定）。

    入参（D-4 · T-20260916-UE5AUTO-HALLBEACON-D1D4-FIX 补齐文档）：
        asset_path (str)：**目标资产路径**，形如 `/Game/Blueprints/BP_HallBeacon`
            （亦接受 `/Game/X/Y.BP_Y` / `/Game/X/Y:BP_Y`，内部按 `.` / `:` 截断取包名）。
            语义 = 「**只**解绑 `globals()` 中 path 指向**该资产**的 UObject 包装器」。
            ⛔ 无参 / 空串**不执行任何删除**：返回 `success=false` + `error`
            （本命令安全硬边界是「只删等于目标包路径的项」，**不提供全量清理语义**）。
        ⚠️ 修正前白名单绑定为 `lambda asset_path, **kw`（无默认值）→ 调用方传
            `params: {}` 时抛裸 `TypeError: missing 1 required positional
            argument: 'asset_path'`（P5 实测）→ 现改默认 `""` + 可读错误。

    🔒 安全硬边界：**只**删除 path == 目标包路径 / 以 `<目标>.` / `<目标>:`
       开头的项；绝不盲删 `globals()` 其它键。返回逐项 audit trail。

    返回：{"success", "asset_path", "package_path", "removed":[{key,path,type}],
           "removed_count", "kept", "gc", "trail", "duration_ms"}
    """
    t0 = time.time()
    out = {"success": False, "asset_path": asset_path, "removed": [], "removed_count": 0,
           "kept": [], "gc": None, "trail": [], "method": "修法3-unpin"}
    pkg = str(asset_path or "").strip()
    pkg = pkg.split(".")[0].split(":")[0]
    if not pkg:
        out["error"] = ("asset_path 为空：本命令必须指定**目标资产路径**"
                        "（如 /Game/Blueprints/BP_HallBeacon）；空参不执行任何删除"
                        "（安全硬边界：只解绑 path 指向该资产的项）")
        return out
    out["package_path"] = pkg
    result_file = os.path.join(os.environ.get("TEMP", "/tmp"),
                               "ue5_unpin_%s.json" % uuid.uuid4().hex[:8])
    try:
        with open(os.path.join(os.path.dirname(__file__), "unpin_console_refs.py.tmpl"),
                  "r", encoding="utf-8") as _f:
            code = _f.read().replace("{TARGET}", repr(pkg)).replace("{RESULT_FILE}", repr(result_file))
        out["trail"].append("target_package: %s" % pkg)
        if not (BRIDGE_AVAILABLE and hasattr(bridge, 'execute_python_on_game_thread')):
            out["error"] = "Bridge DLL 不可用或缺少 execute_python_on_game_thread"
            return out
        _gt_exec_python(code)
        _waited = 0.0
        while (not os.path.exists(result_file)) and _waited < 30.0:
            time.sleep(0.1)
            _waited += 0.1
        out["trail"].append("gt_dispatch_waited: %.1fs" % _waited)
        if os.path.exists(result_file):
            with open(result_file, "r", encoding="utf-8") as _f2:
                _gt = json.load(_f2)
            os.remove(result_file)
            out["removed"] = _gt.get("removed", [])
            out["removed_count"] = len(out["removed"])
            out["kept"] = _gt.get("kept", [])
            out["gc"] = _gt.get("gc")
            out["trail"].append("unpinned: %d" % out["removed_count"])
            out["success"] = True
        else:
            out["error"] = "GT 未产出结果文件（超时 %.1fs）" % _waited
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    out["duration_ms"] = (time.time() - t0) * 1000
    return out


# ══════════════════════════════════════════════════════════
#  v3.0 新能力族（T-20260918-V3-MCP-FUSION）
#  ① PIE 运行时验证（pie_start / pie_stop / pie_state）
#  ② Actor 基础族（find_actors / spawn_actor / set_actor_transform /
#     get_actor_properties，均带 world="editor"|"pie"）
#  ③ 日志查询（logs_read · 游标增量）
#  ④ 批量执行（editor_batch · 白名单命令序列单次执行）
#  设计来源：官方 UE MCP 的"读运行时状态证明行为"验证范式（PIE 世界里
#  查 actor/属性远强于看截图）；本实现全部走既有 GameThread 队列，
#  且所有 UE 属性访问带 hasattr 多候选探测 —— 探测全败时 fail-loud 并
#  回传「试过哪些名字」，绝不静默返回空。
# ══════════════════════════════════════════════════════════

def _ue_ell():
    """取编辑器关卡库对象（EditorLevelLibrary 优先，LevelEditorSubsystem 兜底）。

    ⚠️ 必须在 GameThread 闭包内调用（访问 unreal 模块属性）。
    """
    u = globals().get("unreal")
    if u is None:
        raise BridgeError("unreal 模块不可用", category="pie")
    lib = getattr(u, "EditorLevelLibrary", None)
    if lib is not None:
        return lib, "EditorLevelLibrary"
    lsub = getattr(u, "LevelEditorSubsystem", None)
    ges = getattr(u, "get_editor_subsystem", None)
    if lsub is not None and callable(ges):
        try:
            return ges(lsub), "LevelEditorSubsystem"
        except Exception as e:
            raise BridgeError("LevelEditorSubsystem 获取失败: %s" % e, category="pie")
    raise BridgeError("EditorLevelLibrary / LevelEditorSubsystem 均不可用",
                      category="pie",
                      suggestion="确认编辑器已完整加载（非 -nullrhi 无头模式）")


def _ue_lib_candidates():
    """PIE 相关 API 的跨库候选链 [(lib_name, obj), ...]（GameThread 内调用）。

    实测（UE 5.1.1 · 2026-09-18）：PIE 同名能力分散在不同库 ——
      · EditorLevelLibrary      : editor_play_simulate / editor_end_play
      · LevelEditorSubsystem    : editor_play_simulate / editor_request_end_play /
                                  is_in_play_in_editor / editor_play_in_editor
      · UnrealEditorSubsystem   : get_game_world / get_editor_world（新推荐入口）
    单库探测会漏（旧实现只在 EditorLevelLibrary 上找停止 API → 三个候选全 missing）。
    """
    u = globals().get("unreal")
    out = []
    if u is None:
        return out
    lib = getattr(u, "EditorLevelLibrary", None)
    if lib is not None:
        out.append(("EditorLevelLibrary", lib))
    ges = getattr(u, "get_editor_subsystem", None)
    if callable(ges):
        for cls_name in ("LevelEditorSubsystem", "UnrealEditorSubsystem"):
            cls = getattr(u, cls_name, None)
            if cls is None:
                continue
            try:
                out.append((cls_name, ges(cls)))
            except Exception:
                continue
    return out


def _ue_call_any(names, *args):
    """跨库候选链调用：返回 (result, "Lib.name", tried_notes)。

    全部失败 → (None, "", notes)；notes 含每个 (lib,name) 的缺失/异常原因。
    """
    tried = []
    for lib_name, obj in _ue_lib_candidates():
        for n in names:
            fn = getattr(obj, n, None)
            if not callable(fn):
                tried.append("%s.%s:missing" % (lib_name, n))
                continue
            try:
                return fn(*args), "%s.%s" % (lib_name, n), tried
            except Exception as e:
                tried.append("%s.%s:%s" % (lib_name, n, type(e).__name__))
    return None, "", tried


def _ue_try_call(obj, names, *args):
    """按候选名依次调用 obj.<name>(*args)。

    返回 (result, used_name, tried_notes)；全部失败 → (None, "", notes)。
    """
    tried = []
    for n in names:
        fn = getattr(obj, n, None)
        if not callable(fn):
            tried.append(n + ":missing")
            continue
        try:
            return fn(*args), n, tried
        except Exception as e:
            tried.append("%s:%s" % (n, type(e).__name__))
    return None, "", tried


def _ue_pie_world():
    """取当前 PIE/Simulate 世界对象；不在 PIE 中 → None（GameThread 内调用）。

    跨库候选（实测：EditorLevelLibrary.get_game_world 5.1 可用但已弃用；
    UnrealEditorSubsystem.get_game_world 为新推荐入口）。
    """
    w, _used, _tried = _ue_call_any(("get_game_world", "editor_get_game_world"))
    return w


def _ue_scene_world(world: str):
    """world="pie" → PIE 世界（不在 PIE → fail-loud）；world="editor" → 编辑器世界。"""
    if str(world or "editor").lower() == "pie":
        w = _ue_pie_world()
        if w is None:
            raise BridgeError(
                "当前不在 PIE / Simulate 中（取不到 PIE 世界）",
                category="pie",
                suggestion="先调 pie_start，再读 pie_state 确认 is_playing=true")
        return w, "pie"
    w, _used, tried = _ue_call_any(("get_editor_world",))
    if w is None:
        raise BridgeError("取不到编辑器世界对象（get_editor_world 全库不可用：%s）"
                          % ",".join(tried), category="pie")
    return w, "editor"


def _ue_all_actors(world_obj):
    """枚举世界内全部 Actor（GameplayStatics 优先，EditorLevelLibrary 兜底）。"""
    u = unreal
    gs = getattr(u, "GameplayStatics", None)
    fn = getattr(gs, "get_all_actors_of_class", None) if gs is not None else None
    if callable(fn):
        try:
            actors = fn(world_obj, getattr(u, "Actor"))
            if actors is not None:
                return list(actors)
        except Exception:
            pass
    lib, _name = _ue_ell()
    fn2 = getattr(lib, "get_all_level_actors", None)
    if callable(fn2):
        return list(fn2() or [])
    raise BridgeError("枚举 Actor 的两个通道均不可用（get_all_actors_of_class / "
                      "get_all_level_actors）", category="actor_query")


def _ue_find_actor_by_name(world_obj, name: str):
    """按名称定位 Actor：精确 → 唯一子串；歧义/未命中 → fail-loud（绝不猜）。"""
    actors = _ue_all_actors(world_obj)
    exact = [a for a in actors if a.get_name() == name]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        # 同名多实例（PIE 中 UEDPIE_0_ 前缀剥离后可能撞名）→ 回传路径清单
        paths = [str(a.get_path_name()) for a in exact[:10]]
        raise BridgeError(
            "Actor 同名命中 %d 个，拒绝猜测: %s → %s" % (len(exact), name, paths),
            category="actor_ambiguous")
    sub = [a for a in actors if name and name in a.get_name()]
    if len(sub) == 1:
        return sub[0]
    if len(sub) > 1:
        names = [a.get_name() for a in sub[:10]]
        raise BridgeError(
            "Actor 名称子串命中 %d 个，拒绝猜测: %s → %s" % (len(sub), name, names),
            category="actor_ambiguous")
    raise BridgeError("Actor 未找到: %s" % name, category="actor_not_found")


def _ue_actor_brief(a, with_location=True):
    """Actor → 简报 dict（字段读取失败静默省略，不做假值）。"""
    out = {"name": "", "class": "", "path": ""}
    try:
        out["name"] = str(a.get_name())
    except Exception:
        pass
    try:
        out["class"] = str(a.get_class().get_name())
    except Exception:
        pass
    try:
        out["path"] = str(a.get_path_name())
    except Exception:
        pass
    if with_location:
        try:
            loc = a.get_actor_location()
            out["location"] = [round(loc.x, 2), round(loc.y, 2), round(loc.z, 2)]
        except Exception:
            pass
    # v3.0：mobility 诊断（PIE 中 Static 组件不可位移 —— 运行时报错排查关键信息）
    try:
        rc = getattr(a, "root_component", None)
        if rc is not None:
            out["mobility"] = str(rc.get_editor_property("mobility"))
    except Exception:
        pass
    return out


# ── ① PIE 运行时验证 ─────────────────────────────────────

def _cmd_pie_start(mode: str = "simulate") -> dict:
    """启动 PIE/Simulate（异步发起；用 pie_state 轮询确认）。

    mode: "simulate"（默认，SIE，不占用玩家控制器，更安全）| "pie"（完整 PIE）。
    """
    mode = str(mode or "simulate").lower()
    if mode not in ("simulate", "pie"):
        raise BridgeError('mode 非法: %r（可选 "simulate" | "pie"）' % mode,
                          category="param_type")

    def _do():
        if mode == "pie":
            # 实测 5.1：EditorLevelLibrary 无 editor_play_in_editor，LevelEditorSubsystem 有
            # 实测 5.8（2026-10-24 真机）：editor_play_in_editor 被移除，5.8 头文件
            #   LevelEditorSubsystem.h:78 改为 EditorRequestBeginPlay() —— 候选按新引擎优先
            res, used, tried = _ue_call_any(("editor_request_begin_play",
                                             "editor_play_in_editor"))
        else:
            res, used, tried = _ue_call_any(("editor_play_simulate",))
        if used == "":
            raise BridgeError(
                "PIE 启动 API 不可用（跨库试过: %s）" % ",".join(tried),
                category="pie",
                suggestion=("本引擎可能不支持完整 PIE 启动：UE 5.1 实测只有 "
                            "EditorLevelSubsystem.EditorPlaySimulate（无 PIE 启动 API）；"
                            "UE 5.8 才有 EditorRequestBeginPlay。请改用 "
                            "pie_start{mode:'simulate'}（5.1/5.8 均可），"
                            "或做交互验证时用 simulate + 手动操作"))
        return {"requested": mode, "api": used}

    out = _run_on_game_thread_sync(_do, timeout=120.0)
    out["hint"] = ("PIE 为异步启动；轮询 pie_state 直到 is_playing=true。"
                   "⚠️ 编辑器失焦会暂停 PIE（官方 MCP 同一现象）→ 测行为时保持"
                   "编辑器前台，或优先读状态而非定时等待。")
    return {"success": True, "ok": True, "data": out}


def _cmd_pie_stop() -> dict:
    """请求结束 PIE/Simulate（跨库候选：实测 5.1 停止名是 editor_end_play / 子系统
    editor_request_end_play；旧实现只探 editor_request_end_play_map 等 → 全 missing）。"""
    def _do():
        res, used, tried = _ue_call_any(("editor_end_play",
                                         "editor_request_end_play",
                                         "editor_request_end_play_map",
                                         "editor_end_play_map"))
        if used == "":
            raise BridgeError("PIE 停止 API 不可用（跨库试过: %s）" % ",".join(tried),
                              category="pie")
        return {"api": used}

    out = _run_on_game_thread_sync(_do, timeout=60.0)
    return {"success": True, "ok": True, "data": out}


def _cmd_pie_state() -> dict:
    """读取 PIE/Simulate 运行状态（is_playing / 世界 / 暂停 / 游戏时间）。"""
    def _do():
        u = unreal
        out = {"is_playing": None, "is_playing_api": "",
               "world": None, "paused": None, "time_seconds": None,
               "probe_notes": []}
        res, used, tried = _ue_call_any(("editor_is_in_play_in_editor",
                                         "is_in_play_in_editor",
                                         "editor_is_in_play"))
        if used:
            out["is_playing"] = bool(res)
            out["is_playing_api"] = used
        else:
            out["probe_notes"].extend(tried)
        gw = _ue_pie_world()
        if gw is not None:
            out["is_playing"] = True if out["is_playing"] is None else out["is_playing"]
            try:
                out["world"] = str(gw.get_name())
            except Exception:
                out["world"] = "<world>"
            gs = getattr(u, "GameplayStatics", None)
            if gs is not None:
                fn = getattr(gs, "is_game_paused", None)
                if callable(fn):
                    try:
                        out["paused"] = bool(fn(gw))
                    except Exception:
                        pass
                fn = getattr(gs, "get_time_seconds", None)
                if callable(fn):
                    try:
                        out["time_seconds"] = round(float(fn(gw)), 3)
                    except Exception:
                        pass
        else:
            if out["is_playing"] is None:
                out["is_playing"] = False
                out["is_playing_api"] = "<无探测 API；按无 PIE 世界判定>"
            out["probe_notes"].append("game_world=None（未在 PIE/Simulate）")
        return out

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": out}


# ── ② Actor 基础族 ───────────────────────────────────────

def _cmd_find_actors(actor_class: str = "", name_filter: str = "",
                     world: str = "editor", limit: int = 100) -> dict:
    """枚举世界 Actor（world="editor"|"pie"），支持类名/名称子串过滤。"""
    try:
        limit = max(1, min(1000, int(limit)))
    except (TypeError, ValueError):
        limit = 100
    cls_f = str(actor_class or "").strip().lower()
    name_f = str(name_filter or "").strip().lower()

    def _do():
        w, wname = _ue_scene_world(world)
        actors = _ue_all_actors(w)
        out = []
        for a in actors:
            brief = _ue_actor_brief(a)
            if cls_f and cls_f not in brief.get("class", "").lower():
                continue
            if name_f and name_f not in brief.get("name", "").lower():
                continue
            out.append(brief)
            if len(out) >= limit:
                break
        return {"world": wname, "total_scanned": len(actors),
                "count": len(out), "actors": out}

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": out}


def _cmd_spawn_actor(actor_class: str, location=None, rotation=None,
                     world: str = "editor", mobility: str = "") -> dict:
    """按类生成 Actor 到指定世界（默认编辑器世界）。

    mobility（v3.0 新增）：""（不改）| "static" | "stationary" | "movable" ——
    PIE 运行时验证需要位移 Actor（如运行时传送）时必须 "movable"：
    默认 Static 组件在 PIE 中拒绝位移（本工具回读比对会 fail-loud 拦住）。
    """
    _pywrap_require_str("actor_class", actor_class)
    _raw_cls, _warnings = _pywrap_guard_engine_class_path(
        actor_class, "actor_class", "actor_spawn")
    loc3 = list(location or [0.0, 0.0, 0.0])
    rot3 = list(rotation or [0.0, 0.0, 0.0])
    if len(loc3) != 3 or len(rot3) != 3:
        raise BridgeError("location / rotation 必须是 [x, y, z] 三元组",
                          category="param_type")
    _mob = str(mobility or "").strip().lower()
    if _mob and _mob not in ("static", "stationary", "movable"):
        raise BridgeError('mobility 非法: %r（可选 "" | static | stationary | movable）'
                          % mobility, category="param_type")

    def _do():
        u = unreal
        cls = u.load_class(None, _raw_cls)
        if cls is None:
            raise BridgeError("类加载失败: %s" % _raw_cls, category="actor_spawn")
        vec = u.Vector(float(loc3[0]), float(loc3[1]), float(loc3[2]))
        # UE Rotator 位置构造口径 = (roll, pitch, yaw)；文档契约是 [pitch, yaw, roll]
        # → 必须用关键字构造，否则 pitch/yaw 静默互换（v3.2 实测发现，回读比对抓获）
        rot = u.Rotator(roll=float(rot3[2]), pitch=float(rot3[0]),
                        yaw=float(rot3[1]))
        w, wname = _ue_scene_world(world)
        # PIE 世界生成走 GameplayStatics（EditorLevelLibrary 只面向编辑器世界）
        spawned = None
        if wname == "pie":
            gs = getattr(u, "GameplayStatics", None)
            fn = getattr(gs, "begin_spawn_actor_from_class", None) if gs else None
            if callable(fn):
                spawned = fn(w, cls, vec, rot)
        if spawned is None:
            lib, _n = _ue_ell()
            fn = getattr(lib, "spawn_actor_from_class", None)
            if not callable(fn):
                raise BridgeError("spawn_actor_from_class 不可用（EditorLevelLibrary）",
                                  category="actor_spawn")
            spawned = fn(cls, vec, rot)
        if spawned is None:
            raise BridgeError("生成返回 None（未生成任何 Actor）", category="actor_spawn")
        if _mob:
            _mob_enum = {"static": "STATIC", "stationary": "STATIONARY",
                         "movable": "MOVABLE"}[_mob]
            rc = getattr(spawned, "root_component", None)
            fn = getattr(rc, "set_mobility", None) if rc is not None else None
            if not callable(fn):
                raise BridgeError("root_component.set_mobility 不可用（无法设置 mobility=%s）"
                                  % _mob, category="actor_spawn")
            try:
                target = getattr(u.ComponentMobility, _mob_enum)
            except Exception:
                target = _mob_enum
            fn(target)
            # 回读比对（fail-loud：设置未生效绝不谎报）
            try:
                got = str(rc.get_editor_property("mobility"))
            except Exception:
                got = ""
            if _mob_enum not in got.upper():
                raise BridgeError("mobility 设置未生效：请求 %s，回读 %r"
                                  % (_mob, got), category="actor_spawn")
        brief = _ue_actor_brief(spawned)
        brief["world"] = wname
        return brief

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": out, "warnings": _warnings}


def _cmd_set_actor_transform(actor_name: str, location=None, rotation=None,
                             world: str = "editor") -> dict:
    """设置 Actor 位置/旋转（world="editor"|"pie"，PIE 中即运行时传送）。"""
    _pywrap_require_str("actor_name", actor_name)
    if location is None and rotation is None:
        raise BridgeError("location 与 rotation 至少给一个", category="param_type")
    loc3 = list(location) if location is not None else None
    rot3 = list(rotation) if rotation is not None else None
    if loc3 is not None and len(loc3) != 3:
        raise BridgeError("location 必须是 [x, y, z]", category="param_type")
    if rot3 is not None and len(rot3) != 3:
        raise BridgeError("rotation 必须是 [pitch, yaw, roll]", category="param_type")

    def _do():
        u = unreal
        w, wname = _ue_scene_world(world)
        a = _ue_find_actor_by_name(w, actor_name)
        applied = {}
        if loc3 is not None:
            vec = u.Vector(float(loc3[0]), float(loc3[1]), float(loc3[2]))
            # v3.0 实测修正（2026-09-18 · dev-project · PIE）：传送必须 teleport=True ——
            #   旧实现 (vec, False, False) 在 PIE 中**不生效但零报错**（命令报
            #   success=true、回读坐标未变，属"返回值说谎"族）。候选顺序：
            #   teleport=True → sweep=False,teleport=False → 单参。
            res, used, tried = _ue_try_call(
                a, ("set_actor_location",), vec, False, True)
            if used == "":
                res, used, tried = _ue_try_call(
                    a, ("set_actor_location",), vec, False, False)
            if used == "":
                res, used, tried = _ue_try_call(a, ("set_actor_location",), vec)
            if used == "":
                raise BridgeError("set_actor_location 调用失败（试过: %s）"
                                  % ",".join(tried), category="actor_transform")
            applied["location_api"] = used
        if rot3 is not None:
            # UE Rotator 位置构造口径 = (roll, pitch, yaw)；文档契约是 [pitch, yaw, roll]
            # → 必须用关键字构造，否则 pitch/yaw 静默互换（v3.2 实测发现，回读比对抓获）
            rot = u.Rotator(roll=float(rot3[2]), pitch=float(rot3[0]),
                            yaw=float(rot3[1]))
            res, used, tried = _ue_try_call(
                a, ("set_actor_rotation",), rot, False)
            if used == "":
                res, used, tried = _ue_try_call(a, ("set_actor_rotation",), rot)
            if used == "":
                raise BridgeError("set_actor_rotation 调用失败（试过: %s）"
                                  % ",".join(tried), category="actor_transform")
            applied["rotation_api"] = used
        # ── 设置后回读**比对**（v3.0：绝不接受"返回 None 即成功"的无验证路径）──
        brief = _ue_actor_brief(a)
        try:
            rot_now = a.get_actor_rotation()
            brief["rotation"] = [round(rot_now.pitch, 2), round(rot_now.yaw, 2),
                                 round(rot_now.roll, 2)]
        except Exception:
            pass
        _mismatch = []
        if loc3 is not None:
            _loc_now = brief.get("location")
            if _loc_now is None:
                _mismatch.append("location 回读不可用（无法验证是否生效）")
            elif any(abs(_loc_now[i] - float(loc3[i])) > 1.0 for i in range(3)):
                _mismatch.append("location 回读=%s ≠ 请求=%s（设置未生效）"
                                 % (_loc_now, [float(v) for v in loc3]))
        if rot3 is not None:
            _rot_now = brief.get("rotation")
            if _rot_now is None:
                _mismatch.append("rotation 回读不可用（无法验证是否生效）")
            elif any(abs(_rot_now[i] - float(rot3[i])) > 1.0 for i in range(3)):
                _mismatch.append("rotation 回读=%s ≠ 请求=%s（设置未生效）"
                                 % (_rot_now, [float(v) for v in rot3]))
        if _mismatch:
            raise BridgeError(
                "set_actor_transform 未生效（回读比对失败）: %s；world=%s "
                "（PIE 中传送需 teleport=True，已优先尝试；仍失败请检查该 Actor "
                "是否被物理/附着约束限制）" % ("; ".join(_mismatch), wname),
                category="actor_transform")
        brief["world"] = wname
        brief["applied"] = applied
        brief["verified"] = True
        return brief

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": out}


#: 场景内 command 动作默认拒绝的高危命令（需显式 allow_destructive）
_SCENARIO_DESTRUCTIVE_COMMANDS = frozenset({
    "delete_asset", "agent_undo", "batch_asset_ops", "datatable_write_csv",
    "datatable_create", "apply_graph", "rollback_task_assets",
})


def _scenario_is_destructive(cmd: str, params: dict) -> bool:
    """场景内 command 动作的高危判定（默认拒绝，需显式 allow_destructive）。"""
    params = params or {}
    if cmd in _SCENARIO_DESTRUCTIVE_COMMANDS:
        return True
    if cmd == "apply_graph" and (params.get("replace") or params.get("confirm")):
        return True
    if cmd in ("batch_asset_ops", "datatable_write_csv", "datatable_create") \
            and params.get("confirm"):
        return True
    if cmd == "session_lock_acquire" and params.get("force"):
        return True
    return False


def _cmd_verify_scenario(name: str, steps, start_pie: bool = True,
                         pie_mode: str = "simulate", stop_pie: bool = True,
                         junit_path: str = "", stop_on_failure: bool = True,
                         timeout_s: float = 300.0,
                         allow_destructive: bool = False) -> dict:
    """场景化验收（v6.2/v6.5）：按脚本跑 PIE，逐步断言，产出 JUnit 证据。

    步骤类型（steps 数组，最多 50；type 与 action 两种写法等价）：
      {"type":"wait","seconds":0.5}
      {"type":"assert","actor_name":"A","property_name":"hp","expected":"1",
       "match":"exact","tolerance":0}
      {"type":"screenshot","out_path":"scenario_a.png"}
      {"type":"log_contains","pattern":"Portal entered","timeout":5,
       "log_file":"..."}
      {"type":"command","command":"material_compile",
       "params":{"material_path":"/Game/M_X"}, "allow_destructive":false}
    顶层 start_pie/stop_pie 控制 PIE 生命周期（默认 simulate 起、结束停）。

    v6.5 安全：command 动作是 opt-in 的；高危命令默认拒绝，
    需场景级 allow_destructive=true 或步骤级 allow_destructive=true。
    整个场景有总超时（默认 300s，上限 1800s），防 wait/轮询无限拖。

    ⚠️ 必须在 HTTP 线程执行（等待/轮询会阻塞帧）；/command 与 MCP 已特判。
    返回 verified = 全部 assert/log/command 通过；JUnit 写到项目 Saved 或用户目录。
    """
    if _GT_TASK_RUNNING:
        raise BridgeError(
            "verify_scenario 不能在 GT 任务内运行（等待/轮询会阻塞 PIE 帧）—— "
            "请单独调用，不要放进 editor_batch", category="scenario")
    name = str(name or "").strip() or "scenario"
    if not isinstance(steps, list) or not steps:
        raise BridgeError("steps 必须为非空数组", category="param_type")
    if len(steps) > 50:
        raise BridgeError("steps 上限 50（场景请拆小）", category="param_type")
    timeout_s = _finite_float(timeout_s, 300.0, 1.0, 1800.0)
    t0 = time.time()
    started = False
    stopped = False
    result_steps = []

    def _step_ok(entry):
        entry["status"] = "passed"
        entry["success"] = True

    def _step_fail(entry, msg):
        entry["status"] = "failed"
        entry["success"] = False
        entry["error"] = str(msg)

    if start_pie:
        r = _gt_run_handler(_cmd_pie_start, pie_mode, timeout=120.0) or {}
        if not (r.get("success") or r.get("ok")):
            raise BridgeError(
                "场景启动 PIE 失败: %s" % (r.get("error") or r),
                category="scenario", suggestion="检查 pie_mode（5.1 用 simulate）")
        started = True

    try:
        for i, raw in enumerate(steps):
            entry = {"index": i + 1, "ts": time.strftime("%H:%M:%S")}
            if not isinstance(raw, dict):
                _step_fail(entry, "步骤不是对象")
                result_steps.append(entry)
                if stop_on_failure:
                    break
                continue
            if time.time() - t0 > timeout_s:
                entry["type"] = "timeout"
                entry["name"] = "场景总超时"
                _step_fail(entry, "场景总超时（%.0fs）—— 剩余步骤未执行" % timeout_s)
                entry["duration_ms"] = 0.0
                result_steps.append(entry)
                break
            kind = str(raw.get("type") or raw.get("action") or "").strip().lower()
            kind = {"assert_property": "assert", "assert_log": "log_contains",
                    "start": "pie_start", "stop": "pie_stop"}.get(kind, kind)
            entry["type"] = kind
            entry["name"] = str(raw.get("name") or kind)
            st = time.time()
            try:
                if kind == "wait":
                    sec = max(0.0, min(60.0, float(raw.get("seconds") or 0)))
                    time.sleep(sec)
                    entry["detail"] = {"seconds": sec}
                    _step_ok(entry)
                elif kind == "assert":
                    r = _gt_run_handler(
                        _cmd_verify_behavior,
                        str(raw.get("actor_name") or ""),
                        str(raw.get("property_name") or ""),
                        raw.get("expected", ""),
                        "pie", str(raw.get("match") or "exact"),
                        raw.get("tolerance", 0.0), False, "",
                        timeout=60.0) or {}
                    entry["detail"] = {
                        "actor": r.get("actor"), "property": r.get("property"),
                        "expected": r.get("expected"), "actual": r.get("actual"),
                    }
                    if r.get("verified"):
                        _step_ok(entry)
                    else:
                        _step_fail(entry, r.get("error") or "断言未通过")
                elif kind == "screenshot":
                    shot = _capture_cmd_dispatch("capture_pie",
                                                 out_path=str(raw.get("out_path") or ""))
                    entry["detail"] = {"saved_path":
                                       (shot.get("data") or {}).get("saved_path")}
                    if shot.get("success") or shot.get("ok"):
                        _step_ok(entry)
                    else:
                        _step_fail(entry, shot.get("error") or "截图失败")
                elif kind == "log_contains":
                    pattern = str(raw.get("pattern") or "").strip()
                    if not pattern:
                        raise BridgeError("log_contains 缺 pattern", category="param_type")
                    if len(pattern) > 200:
                        raise BridgeError("log_contains pattern 上限 200 字符",
                                          category="param_type")
                    timeout = max(0.1, min(30.0, float(raw.get("timeout") or 5)))
                    log_file = str(raw.get("log_file") or "")
                    deadline = time.time() + timeout
                    found = False
                    while time.time() < deadline:
                        # 字面量匹配（re.escape）—— 防止用户正则触发 ReDoS
                        rr = _gt_run_handler(
                            _cmd_logs_read, 300, "", re.escape(pattern), 0,
                            524288, log_file, timeout=30.0) or {}
                        n = int(((rr.get("data") or {}).get("count")) or 0)
                        if n > 0:
                            found = True
                            break
                        time.sleep(0.5)
                    entry["detail"] = {"pattern": pattern, "found": found,
                                       "log_file": log_file or None}
                    if found:
                        _step_ok(entry)
                    else:
                        _step_fail(entry, "日志中未出现: %s" % pattern)
                elif kind == "pie_start":
                    mode = str(raw.get("mode") or raw.get("pie_mode") or pie_mode)
                    raw_r = _gt_run_handler(_cmd_pie_start, mode, timeout=60.0) or {}
                    ok_r = not (raw_r.get("success") is False
                                or raw_r.get("ok") is False)
                    entry["detail"] = {"mode": mode, "result": raw_r}
                    if ok_r:
                        started, stopped = True, False
                        _step_ok(entry)
                    else:
                        _step_fail(entry, raw_r.get("error") or "PIE 启动失败")
                elif kind == "pie_stop":
                    raw_r = _gt_run_handler(_cmd_pie_stop, timeout=30.0) or {}
                    ok_r = not (raw_r.get("success") is False
                                or raw_r.get("ok") is False)
                    entry["detail"] = {"result": raw_r}
                    if ok_r:
                        stopped = True
                        _step_ok(entry)
                    else:
                        _step_fail(entry, raw_r.get("error") or "PIE 停止失败")
                elif kind == "command":
                    cmd = str(raw.get("command") or "").strip()
                    if cmd not in _COMMAND_WHITELIST:
                        raise BridgeError("未知命令: %s" % cmd, category="param_type")
                    # 场景内禁止递归 + 禁止用 batch 绕过高危检查
                    if cmd in ("verify_scenario", "editor_batch"):
                        raise BridgeError(
                            "场景内不允许调用 %s（递归/批量会绕过高危检查）" % cmd,
                            category="scenario_safety")
                    params = raw.get("params") or {}
                    if not isinstance(params, dict):
                        raise BridgeError("command.params 必须是对象",
                                          category="param_type")
                    if _scenario_is_destructive(cmd, params) and not (
                            allow_destructive or raw.get("allow_destructive")):
                        raise BridgeError(
                            "场景默认禁止高危命令 %s（设置 allow_destructive=true "
                            "才放行）" % cmd, category="scenario_safety")
                    # lambda 包一层：params 里若也有 timeout 键，不能覆盖
                    # _gt_run_handler 自己的超时参数
                    raw_r = _gt_run_handler(
                        lambda: _dispatch_with_tx(
                            cmd, _COMMAND_WHITELIST[cmd], **params),
                        timeout=300.0) or {}
                    ok_r = not (raw_r.get("success") is False
                                or raw_r.get("ok") is False
                                or raw_r.get("_fail_loud"))
                    entry["detail"] = {
                        "command": cmd,
                        "success": raw_r.get("success"),
                        "verified": raw_r.get("verified"),
                        "error": raw_r.get("error"),
                    }
                    if ok_r and cmd == "pie_start":
                        started, stopped = True, False
                    if ok_r and cmd == "pie_stop":
                        stopped = True
                    if ok_r:
                        _step_ok(entry)
                    else:
                        _step_fail(entry, raw_r.get("error") or "命令失败")
                else:
                    raise BridgeError("未知步骤类型: %r（wait/assert/screenshot/"
                                      "log_contains/command/pie_start/pie_stop）"
                                      % kind, category="param_type")
            except Exception as e:  # noqa: BLE001 —— 单步失败不该炸掉整场
                _step_fail(entry, "%s: %s" % (type(e).__name__, e))
            entry["duration_ms"] = round((time.time() - st) * 1000, 1)
            result_steps.append(entry)
            if entry["status"] == "failed" and stop_on_failure:
                break
    finally:
        # 只要场景启动过 PIE 且未显式停，就清理（含 command 动作启停）
        if stop_pie and started and not stopped:
            try:
                _gt_run_handler(_cmd_pie_stop, timeout=60.0)
                stopped = True
            except Exception:
                pass

    executed = len(result_steps)
    for i in range(executed, len(steps)):
        raw_skip = steps[i] if isinstance(steps[i], dict) else {}
        result_steps.append({"index": i + 1, "status": "skipped",
                             "type": str(raw_skip.get("type")
                                         or raw_skip.get("action") or "?"),
                             "name": "（未执行）"})
    failed = [x for x in result_steps if x.get("status") == "failed"]
    verified = not failed

    # ── JUnit XML ──
    import xml.etree.ElementTree as ET
    suite = ET.Element("testsuite", {
        "name": "ue5.scenario." + name,
        "tests": str(len(result_steps)),
        "failures": str(len(failed)),
        "errors": "0",
        "time": "%.3f" % (time.time() - t0),
    })
    for x in result_steps:
        case = ET.SubElement(suite, "testcase", {
            "classname": "ue5.scenario", "name": x.get("name") or x.get("type"),
            "time": "%.3f" % ((x.get("duration_ms") or 0) / 1000.0)})
        if x.get("status") == "failed":
            f = ET.SubElement(case, "failure", {"message": str(x.get("error"))[:300]})
            f.text = str(x.get("error"))
        elif x.get("status") == "skipped":
            ET.SubElement(case, "skipped")
    xml_text = ET.tostring(suite, encoding="unicode")

    import tempfile as _tempfile
    try:
        _saved = _gt_run_handler(
            lambda: unreal.Paths.convert_relative_path_to_full(
                unreal.Paths.project_saved_dir()), timeout=15.0)
        default_dir = os.path.join(str(_saved), "ue5-automation", "scenarios")
    except Exception:
        default_dir = os.path.join(
            os.environ.get("APPDATA") or os.path.expanduser("~"),
            "ue5_bridge", "scenarios")
    if junit_path:
        candidate = str(junit_path)
        if not os.path.isabs(candidate):
            candidate = os.path.join(default_dir, candidate)
        def _norm_fs_path(value):
            # realpath：解析 symlink/junction/short path；CI 的 pytest tmp
            # 可能经 junction 落到另一个真实路径，单用 abspath 会误判越界。
            return os.path.normcase(os.path.realpath(os.path.abspath(str(value))))

        norm = _norm_fs_path(candidate)
        temp_dirs = []
        for _t in (_tempfile.gettempdir(), os.environ.get("TEMP"),
                   os.environ.get("TMP"), os.environ.get("TMPDIR"),
                   os.environ.get("RUNNER_TEMP")):
            if _t:
                temp_dirs.append(_norm_fs_path(_t))
        roots = [
            _norm_fs_path(default_dir),
            _norm_fs_path(os.path.join(
                os.environ.get("APPDATA") or os.path.expanduser("~"),
                "ue5_bridge")),
        ]
        for _t in temp_dirs:
            roots.append(_norm_fs_path(os.path.join(_t, "ue5_bridge")))
            # 系统临时目录本身（本地测试/CI 临时产物；不含用户文档）
            roots.append(_t)
        if not any(norm == r or norm.startswith(r + os.sep) for r in roots):
            raise BridgeError(
                "junit_path 越界（只允许项目 Saved / %%APPDATA%%/ue5_bridge / "
                "系统临时目录）: %s" % junit_path, category="path_escape")
        out = norm
    else:
        out = os.path.join(default_dir,
                           "%s-%s.xml" % (re.sub(r"[^A-Za-z0-9_.-]+", "_", name),
                                          time.strftime("%Y%m%d-%H%M%S")))
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(xml_text)
        junit_written = True
    except OSError as e:
        junit_written = False
        out = "写入失败: %s" % e

    return {
        "success": verified,
        "ok": verified,
        "verified": verified,
        "name": name,
        "failed_count": len(failed),
        "steps": result_steps,
        "junit_path": out,
        "junit_written": junit_written,
        "duration_s": round(time.time() - t0, 2),
        "timeout_s": timeout_s,
        "allow_destructive": bool(allow_destructive),
        "started_pie": started,
        "pie_stopped": stopped,
        "error": (None if verified else
                  "%d 个步骤失败（见 steps/junit）" % len(failed)),
        "_fail_loud": not verified,
    }


def _cmd_verify_behavior(actor_name: str, property_name: str, expected,
                         world: str = "pie", match: str = "exact",
                         tolerance: float = 0.0, capture: bool = True,
                         out_path: str = "") -> dict:
    """行为验证：读运行时属性并与期望值比对，可选截图作为证据（v4.9）。

    这是"语义验证"的最小闭环：编译通过只证明图合法，属性/日志/画面才证明行为。
    · world="pie"（默认）→ 必须 PIE/Simulate 在跑（fail-loud，提示 pie_start）；
    · match="exact"（默认，支持数值 tolerance）| "contains"（子串，忽略大小写）；
    · capture=true → 同时截图（PIE 画面；world=editor 时截视口），作为 evidence。
    返回 verified 为唯一判据；screenshot 失败只告警，不改变 verified。
    """
    _pywrap_require_str("actor_name", actor_name)
    _pywrap_require_str("property_name", property_name)
    world = str(world or "pie").strip().lower()
    if world not in ("pie", "editor"):
        raise BridgeError("world 只支持 'pie' | 'editor'（收到 %r）" % world,
                          category="param_type")
    match = str(match or "exact").strip().lower()
    if match not in ("exact", "contains"):
        raise BridgeError("match 只支持 'exact' | 'contains'（收到 %r）" % match,
                          category="param_type")
    try:
        tol = max(0.0, float(tolerance or 0.0))
    except (TypeError, ValueError):
        tol = 0.0

    if world == "pie":
        state = _cmd_pie_state()
        playing = bool((state.get("data") or {}).get("is_playing"))
        if not playing:
            raise BridgeError(
                "PIE/Simulate 未在运行 —— 行为验证需要运行时世界",
                category="pie_state",
                suggestion="先 pie_start{mode:'simulate' or 'pie'}，再调用 verify_behavior",
            )

    prop = _cmd_get_actor_properties(actor_name, property_name, world)
    data = prop.get("data") if isinstance(prop.get("data"), dict) else {}
    if not prop.get("success") or "value" not in data:
        raise BridgeError(
            "属性读取失败：%s.%s（%s）"
            % (actor_name, property_name, prop.get("error") or "未知原因"),
            category="actor_property",
            suggestion="先用 get_actor_properties{property_name:''} 列候选名",
        )

    actual = data.get("value")
    actual_text = str(actual)
    # get_actor_properties 的 value 是 JSON 序列化形态：字符串会带引号
    # （"WorldSettings"）。断言要按用户直觉比较裸值，这里尝试解包一次。
    try:
        if isinstance(actual, str) and len(actual) >= 2                 and actual[0] == '"' and actual[-1] == '"':
            actual_text = str(json.loads(actual))
    except (ValueError, TypeError):
        pass
    expected_text = str(expected)
    # 期望值也允许带 JSON 引号（用户从回读结果原样复制时）——与裸值等价
    try:
        if len(expected_text) >= 2 and expected_text[0] == '"'                 and expected_text[-1] == '"':
            expected_text = str(json.loads(expected_text))
    except (ValueError, TypeError):
        pass
    verified = False
    if match == "contains":
        verified = expected_text.lower() in actual_text.lower()
    else:
        a_num = e_num = None
        try:
            a_num, e_num = float(actual_text), float(expected_text)
        except (TypeError, ValueError):
            pass
        if a_num is not None and e_num is not None:
            verified = abs(a_num - e_num) <= max(tol, 1e-9)
        else:
            verified = actual_text.strip() == expected_text.strip()

    result: Dict[str, Any] = {
        "success": verified,
        "ok": verified,
        "verified": verified,
        "actor": actor_name,
        "property": property_name,
        "world": data.get("world") or world,
        "expected": expected_text,
        "actual": actual_text,
        "actual_type": data.get("value_type"),
        "match": match,
        "tolerance": tol,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not verified:
        result["error"] = ("行为断言未通过：%s.%s 期望 %r，实际 %r"
                           % (actor_name, property_name, expected_text, actual_text))
        result["_fail_loud"] = True

    if capture:
        try:
            shot_name = str(out_path or "").strip() or ("verify_%s.png" % re.sub(
                r"[^A-Za-z0-9_.-]+", "_", actor_name))
            shot = (_cmd_capture_viewport(shot_name) if world == "editor"
                    else _cmd_capture_pie(shot_name))
            result["screenshot"] = shot
            if isinstance(shot, dict) and shot.get("success") is False:
                result.setdefault("warnings", []).append(
                    "截图失败（不影响 verified）：%s" % shot.get("error"))
        except Exception as e:
            result.setdefault("warnings", []).append(
                "截图异常（不影响 verified）：%s: %s" % (type(e).__name__, e))
    return result


def _cmd_get_actor_properties(actor_name: str, property_name: str = "",
                              world: str = "editor", limit: int = 200) -> dict:
    """读取 Actor 属性：property_name 空 → 列出可选属性名；给定 → 读值+类型。"""
    _pywrap_require_str("actor_name", actor_name)
    try:
        limit = max(1, min(1000, int(limit)))
    except (TypeError, ValueError):
        limit = 200

    def _do():
        w, wname = _ue_scene_world(world)
        a = _ue_find_actor_by_name(w, actor_name)
        prop = str(property_name or "").strip()
        if not prop:
            # 列属性名：dir() 过滤下划线开头 + 常见方法前缀，取前 limit 个
            names = sorted(set(n for n in dir(a) if not n.startswith("_")))
            names = [n for n in names if not n.startswith(("get_", "set_", "add_",
                                                           "remove_", "is_", "k2_"))]
            return {"actor": a.get_name(), "world": wname, "property_count": len(names),
                    "properties": names[:limit],
                    "note": "property_name 为空 → 返回候选名清单；给名读值"}
        # 读值：get_editor_property 优先；失败回退零参 get_<name>
        val = None
        used = ""
        try:
            val = a.get_editor_property(prop)
            used = "get_editor_property"
        except Exception as e1:
            getter = getattr(a, "get_" + prop, None)
            if callable(getter):
                try:
                    val = getter()
                    used = "get_%s()" % prop
                except Exception as e2:
                    raise BridgeError(
                        "属性读取失败: %s（get_editor_property: %s / get_%s: %s）"
                        % (prop, type(e1).__name__, prop, type(e2).__name__),
                        category="actor_property")
            else:
                raise BridgeError(
                    "属性 %r 不可读（get_editor_property: %s，且无 get_%s）"
                    % (prop, type(e1).__name__, prop),
                    category="actor_property",
                    suggestion="先用 property_name=\"\" 列候选名")
        out = {"actor": a.get_name(), "world": wname, "property": prop,
               "value": _safe_json(val), "value_type": type(val).__name__,
               "read_by": used}
        return out

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": out}


# ── ③ 日志查询 ───────────────────────────────────────────

def _allowed_log_roots():
    """允许读取的日志根目录（项目日志 / Saved/Logs / 系统临时目录）。

    v6.4 安全：logs_read 的 log_file 曾被原样 abspath → 可读任意文件
    （包括 %APPDATA%/ue5_bridge/token）。现在只允许这些根目录内的文件。
    """
    roots = []
    u = globals().get("unreal")
    pdet = getattr(u, "Paths", None) if u is not None else None
    if pdet is not None:
        for fn, sub in (("project_log_dir", ""), ("project_saved_dir", "Logs")):
            f = getattr(pdet, fn, None)
            if callable(f):
                try:
                    base = str(f())
                    conv = getattr(pdet, "convert_relative_path_to_full", None)
                    if callable(conv):
                        base = conv(base)
                    roots.append(os.path.abspath(
                        os.path.join(base, sub) if sub else base))
                except Exception:
                    pass
    try:
        roots.append(os.path.abspath(__import__("tempfile").gettempdir()))
    except Exception:
        pass
    # v6.5：显式允许根（`;` 分隔；跨机验证/CI 的临时日志目录）
    for extra in str(os.environ.get("UE5_LOG_ALLOW_PATHS", "")).split(";"):
        extra = extra.strip()
        if extra:
            try:
                roots.append(os.path.abspath(extra))
            except Exception:
                pass
    return [os.path.normcase(r) for r in roots]


def _resolve_log_file(log_file: str = ""):
    """定位 UE 日志文件（显式 > project_log_dir > project/Saved/Logs 推导）。"""
    if log_file:
        p = os.path.abspath(log_file)
        if not os.path.isfile(p):
            return None
        norm = os.path.normcase(p)
        allowed = _allowed_log_roots()
        if not any(norm.startswith(r + os.sep) or norm == r for r in allowed):
            return None
        return p
    u = globals().get("unreal")
    dirs = []
    if u is not None:
        pdet = getattr(u, "Paths", None)

        def _abs(v):
            # v3.2 实测修复（5.8）：Paths 返回相对路径（../../../..），按编辑器
            # CWD 解析会指错盘符 → 统一转绝对（转失败保留原值兜底）。
            try:
                conv = getattr(u.Paths, "convert_relative_path_to_full", None)
                if callable(conv):
                    return conv(v)
            except Exception:
                pass
            return v

        for n in ("project_log_dir", "project_saved_dir"):
            fn = getattr(pdet, n, None) if pdet is not None else None
            if callable(fn):
                try:
                    v = _abs(str(fn()))
                    dirs.append(v if n == "project_log_dir"
                                else os.path.join(v, "Logs"))
                except Exception:
                    pass
        fn = getattr(pdet, "project_dir", None) if pdet is not None else None
        if callable(fn):
            try:
                dirs.append(os.path.join(_abs(str(fn())), "Saved", "Logs"))
            except Exception:
                pass
    for d in dirs:
        if d and os.path.isdir(d):
            try:
                files = [f for f in os.listdir(d) if f.lower().endswith(".log")]
            except OSError:
                continue
            if files:
                files.sort(key=lambda f: os.path.getmtime(os.path.join(d, f)),
                           reverse=True)
                return os.path.join(d, files[0])
    return None


def _cmd_logs_read(lines: int = 200, category: str = "", pattern: str = "",
                   cursor: int = 0, max_bytes: int = 524288,
                   log_file: str = "") -> dict:
    """读取 UE 日志。

    · cursor=0 → 取文件尾部最后 `lines` 行；cursor>0 → 取该字节偏移之后的新增行
      （返回 next_cursor 供增量轮询）。
    · category / pattern 为大小写不敏感的子串 / 正则过滤。
    """
    try:
        lines = max(1, min(2000, int(lines)))
        cursor = max(0, int(cursor or 0))
        max_bytes = max(4096, min(4 * 1024 * 1024, int(max_bytes)))
    except (TypeError, ValueError):
        raise BridgeError("lines / cursor / max_bytes 必须为整数",
                          category="param_type")
    path = _resolve_log_file(log_file)
    if not path:
        raise BridgeError("UE 日志文件不可定位（project_log_dir 不可用且无显式 log_file）",
                          category="logs")
    try:
        size = os.path.getsize(path)
        start = cursor if cursor > 0 else max(0, size - max_bytes)
        if start > size:
            start = 0  # 文件被轮转（新会话新日志）→ 从头
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(start)
            chunk = f.read(max_bytes)
            new_cursor = f.tell()
    except OSError as e:
        raise BridgeError("日志读取失败: %s" % e, category="logs")
    raw = chunk.splitlines()
    tail_mode = cursor <= 0
    if tail_mode and len(raw) > lines:
        raw = raw[-lines:]
    if category:
        c = str(category).lower()
        raw = [l for l in raw if c in l.lower()]
    if pattern:
        import re as _re
        if len(str(pattern)) > 200:
            raise BridgeError("pattern 上限 200 字符", category="param_type")
        # 回溯炸弹常见形态：嵌套量词 (a+)+ / (.*)* / (a*)* —— 直接拒绝
        if _re.search(r"\([^)]*[*+][^)]*\)\s*[*+{]", str(pattern)):
            raise BridgeError(
                "pattern 含嵌套量词（可能触发 ReDoS），请简化或改用字面量",
                category="param_type")
        try:
            rx = _re.compile(str(pattern), _re.IGNORECASE)
        except _re.error as e:
            raise BridgeError("pattern 不是合法正则: %s" % e, category="param_type")
        raw = [l for l in raw if rx.search(l)]
    if len(raw) > lines:
        raw = raw[-lines:]
    return {"success": True, "ok": True,
            "data": {"file": os.path.basename(path), "size": size,
                     "cursor": new_cursor, "next_cursor": new_cursor,
                     "tail_mode": tail_mode, "count": len(raw), "lines": raw,
                     "filters": {"category": category or None,
                                 "pattern": pattern or None}}}


# ── ④ 批量执行 ───────────────────────────────────────────

def _cmd_editor_batch(steps, stop_on_error: bool = True) -> dict:
    """按序执行白名单命令序列（单次请求完成多步操作）。

    steps: [{"command": "<白名单命令>", "params": {...}}, ...]
    任一步 _fail_loud 失败 / 抛异常 → 默认中止（stop_on_error=False 可继续）。
    """
    if not isinstance(steps, list) or not steps:
        raise BridgeError("steps 必须为非空数组（[{command, params}, ...]）",
                          category="param_type")
    if len(steps) > 200:
        raise BridgeError("steps 超过上限 200 条（%d）" % len(steps),
                          category="param_type")
    results = []
    t0 = time.time()
    stopped_early = False
    for i, st in enumerate(steps):
        entry = {"index": i}
        if not isinstance(st, dict):
            entry.update({"success": False, "error": "steps[%d] 非对象" % i})
            results.append(entry)
            stopped_early = bool(stop_on_error)
            if stopped_early:
                break
            continue
        cmd = str(st.get("command") or "").strip()
        params = st.get("params") or {}
        entry["command"] = cmd
        if not isinstance(params, dict):
            entry.update({"success": False, "error": "params 必须为对象"})
            results.append(entry)
            stopped_early = bool(stop_on_error)
            if stopped_early:
                break
            continue
        t1 = time.time()
        if cmd not in _COMMAND_WHITELIST:
            entry.update({"success": False,
                          "error": "Unknown command: %s" % cmd})
            results.append(entry)
            stopped_early = bool(stop_on_error)
            if stopped_early:
                break
            continue
        try:
            r = _dispatch_with_tx(cmd, _COMMAND_WHITELIST[cmd], **params)
            if isinstance(r, dict) and r.get("_fail_loud") and not r.get("success"):
                entry.update({"success": False,
                              "error": str(r.get("error", "")), "result": r})
            else:
                entry.update({"success": True, "result": r})
        except Exception as e:
            entry.update({"success": False,
                          "error": "%s: %s" % (type(e).__name__, e)})
        entry["duration_ms"] = round((time.time() - t1) * 1000, 1)
        results.append(entry)
        if not entry.get("success") and stop_on_error:
            stopped_early = True
            break
    completed = sum(1 for r in results if r.get("success"))
    ok = completed == len(steps) and not stopped_early
    return {"success": ok, "_fail_loud": True, "completed": completed,
            "total": len(steps), "stopped_early": stopped_early,
            "duration_ms": round((time.time() - t0) * 1000, 1),
            "steps": results}


# ── ④b 截图反馈闭环（T-20261024-UE5BRIDGE-SCREENSHOT · v3.7）──
#  目标（P0 规格书）：给 agent 装上"眼睛" —— 修改 → 截图 → 视觉自检 → 修正。
#  三个白名单命令转发到 C++ 桥（bridge.capture_viewport / capture_pie /
#  project_actors_to_screen，实现见 BlueprintCapture.cpp —— v3.7 离线草稿，
#  回家编译后可用）。纪律：
#    · 旧 DLL 无这些 API → 干净降级报错（fail-loud，绝不假成功）；
#    · 输出路径强制约束在 <项目>/Saved/Screenshots/ue5-automation/ 下
#      （权威检查在 C++，宿主侧再做一道入参预检，错误消息更友好）；
#    · FScreenshotRequest（PIE 路线）是【延迟一帧】执行的：截图文件由帧推进
#      产生，而帧推进发生在引擎主循环 = GameThread。【在 GT 上轮询文件 =
#      阻塞帧推进 = 永远等不到（自死锁）】。故 pending 等待一律在
#      【非 GT 线程】完成：本线程非 GT → 内联轮询；本线程是 GT worker
#      （在线 /command、MCP 经 _gt_run_handler 的路径）→ 返回 `_pending_poll`
#      标记，由端点层（execute_command / _mcp_handle_one）在 HTTP 线程上
#      调 _capture_finish_pending 收尾。UEBox 用编辑器定时器异步回调解决
#      同一问题；我们选宿主轮询，因为 bridge API 是同步返回形态。

_CAPTURE_POLL_TIMEOUT_S = 2.0     # 规格书：pending 截图 2s 超时 fail-loud
# PIE 源放宽（真机实测 2026-10-24）：StartPIE 刚返回后的首张截图会撞 PIE 预热
# （shader 编译等帧卡顿），FScreenshotRequest 延迟一帧 + GT 端 PNG 编码可超 2s
# （实测 ~3s+ 才落盘）→ 稳态仍走 2s 规格值，仅 capture_pie 用 8s。
_CAPTURE_PIE_TIMEOUT_S = 8.0
_CAPTURE_POLL_INTERVAL_S = 0.1


def _capture_require_bridge_api(attr: str) -> None:
    """旧 DLL 无截图 API → 干净降级报错（绝不静默降级成别的操作）。"""
    if not (BRIDGE_AVAILABLE and hasattr(bridge, attr)):
        raise BridgeError(
            "C++ Bridge 无 %s API —— 当前 DLL 为 v3.7 之前编译，未含截图闭环。"
            "请用 bridge/BlueprintPythonBridge/Source/build_for_engine.bat 重新编译"
            "并替换 Binaries/Win64/ 下的 DLL 后重启编辑器" % attr,
            category="screenshot")


def _capture_check_out_path(out_path) -> str:
    """宿主侧第一道防线（权威在 C++ 侧前缀检查）：只放行约定目录内的相对路径。

    拒绝：绝对路径（以 / 或盘符开头）与任何 ".." 分量 —— 防路径逃逸。
    """
    p = str(out_path or "").strip().replace("\\", "/")
    if not p:
        return ""
    if p.startswith("/") or (len(p) >= 2 and p[1] == ":") or ".." in p.split("/"):
        raise BridgeError(
            "out_path 越界（防逃逸拒绝）: %r —— 只允许约定目录内的相对路径"
            "（如 'shot_a.png' 或 'cases/shot_a.png'）；落盘目录固定为"
            " <项目>/Saved/Screenshots/ue5-automation/" % out_path,
            category="screenshot")
    return p


def _capture_cmd_dispatch(cmd: str, **params) -> dict:
    """在正确线程执行 capture 白名单命令并完成 pending 收尾（MCP 门面专用）。

    _gt_run_handler：TEST_MODE / 已在 GT → 直呼；在线 HTTP 线程 → 投 GT worker。
    命令体在 GT worker 里遇到 pending 时只挂标记（等文件 = 阻塞帧推进 = 死锁），
    本函数拿到返回后（已回到调用线程，必为非 GT）用 _capture_finish_pending 收尾。
    """
    result = _gt_run_handler(_COMMAND_WHITELIST[cmd], **params)
    return _capture_finish_pending(result)


def _capture_call_bridge_json(attr: str, args_tuple: tuple, timeout: float = 120.0) -> dict:
    """调桥截图 API（恒返回 FString JSON）并解析；ok=false → BridgeError。"""
    _capture_require_bridge_api(attr)
    fn = getattr(bridge, attr)

    def _do():
        return fn(*args_tuple)
    _do.__name__ = "bridge_" + attr  # GT 超时消息可读（_run_on_game_thread_sync 用 __name__）
    raw = _gt_run_handler(_do, timeout=timeout)
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise BridgeError("%s 返回非 JSON（DLL 版本不匹配或 C++ 侧异常）: %r" % (attr, raw),
                          category="screenshot")
    if not data.get("ok"):
        raise BridgeError(str(data.get("error") or "%s 返回 ok=false（无错误文本）" % attr),
                          category="screenshot")
    return data


def _capture_wait_file(path: str, timeout_s: float = _CAPTURE_POLL_TIMEOUT_S) -> dict:
    """轮询截图文件出现且大小稳定（FScreenshotRequest 延迟一帧 + PNG 编码落盘）。

    纪律：必须在【非 GT 线程】调用（文件由帧推进产生；GT 上等待 = 自死锁，
    见本节头注）。超时 → BridgeError（fail-loud，绝不把"没等到"报成成功）。
    """
    t0 = time.time()
    last_size = -1
    stable = 0
    while time.time() - t0 < timeout_s:
        try:
            if os.path.isfile(path):
                size = os.path.getsize(path)
                if size > 0:
                    if size == last_size:
                        stable += 1
                        if stable >= 2:  # 连续 2 次采样大小一致 = 写完（编码器不再追加）
                            return {"bytes": size}
                    else:
                        stable = 0
                    last_size = size
        except OSError:
            pass  # 文件被写入器瞬时占用/删除 → 下一轮再看
        time.sleep(_CAPTURE_POLL_INTERVAL_S)
    raise BridgeError(
        "截图文件 %.1fs 内未就绪: %s —— 编辑器可能失焦（失焦会暂停 PIE 帧推进）"
        "或引擎把截图重定向到了其他目录" % (timeout_s, path),
        category="screenshot")


def _capture_finish_pending(result, _depth: int = 0):
    """完成 GT 上下文里发起、带 `_pending_poll` 标记的截图命令（端点层钩子）。

    【必须在非 GT 线程调用】—— execute_command / _mcp_handle_one 在
    `_gt_run_handler` 返回之后（已在 HTTP 线程）调本函数。
    递归面（深度 3 封顶）：顶层返回体 / `result.result`（ue5_execute_command
    的白名单透传壳）/ `steps[].result`（editor_batch 步骤）。轮询失败 →
    把对应返回体改成 success=false + _fail_loud（继承 D-1 的 fail-loud 传播）。
    """
    if _depth > 3 or not isinstance(result, dict):
        return result

    path = result.get("_pending_poll")
    if path:
        result.pop("_pending_poll", None)
        data = result.get("data") if isinstance(result.get("data"), dict) else None
        try:
            if not isinstance(data, dict):
                raise BridgeError("_pending_poll 标记存在但 data 缺失（内部契约破坏）",
                                  category="screenshot")
            st = _capture_wait_file(
                str(path),
                timeout_s=float(result.get("_pending_poll_timeout") or _CAPTURE_POLL_TIMEOUT_S))
            data["bytes"] = st["bytes"]
            data.pop("pending", None)
        except BridgeError as e:
            # 超时 = fail-loud：绝不把"文件没等到"报成成功（D-1 语义继承）
            result["success"] = False
            result["ok"] = False
            result["_fail_loud"] = True
            result["error"] = str(e)
        return result

    # 容器递归：ue5_execute_command 的 result.result / editor_batch 的 steps[].result
    # 行为验证（v4.9）：screenshot 是证据，失败不翻转 verified
    shot = result.get("screenshot")
    if isinstance(shot, dict) and shot.get("_pending_poll"):
        result["screenshot"] = _capture_finish_pending(shot, _depth + 1)

    inner = result.get("result")
    if isinstance(inner, dict):
        result["result"] = _capture_finish_pending(inner, _depth + 1)
        if result["result"].get("success") is False and result.get("success"):
            # 透传壳把失败顶上去（isError / D-1 判定都读顶层）
            result["success"] = False
            result["error"] = str(result["result"].get("error") or "截图未就绪")
            result["_fail_loud"] = True
    steps = result.get("steps")
    if isinstance(steps, list):
        batch_dirty = False
        for st in steps:
            if isinstance(st, dict) and isinstance(st.get("result"), dict):
                st["result"] = _capture_finish_pending(st["result"], _depth + 1)
                if (st["result"].get("success") is False) and st.get("success"):
                    st["success"] = False
                    st["error"] = str(st["result"].get("error") or "截图未就绪")
                    batch_dirty = True
        if batch_dirty and result.get("success"):
            result["success"] = False
            result["error"] = "批量步骤中存在截图未就绪（fail-loud）"
            result["_fail_loud"] = True
    return result


def _capture_verify_sync_file(data: dict) -> None:
    """同步路线（SceneCapture 落盘后才返回）的防御性读回验证 + bytes 回填。

    C++ 报成功 = 文件已写完（SaveArrayToFile 之后才返回）；文件缺失/0 字节
    属于契约破坏 → fail-loud（绝不拿"桥说成功"当真）。
    """
    _sp = str(data.get("saved_path") or "")
    if not _sp:
        raise BridgeError("桥返回体缺 saved_path（C++ 契约破坏）", category="screenshot")
    try:
        size = os.path.getsize(_sp)
    except OSError as e:
        raise BridgeError("截图落盘验证失败（桥报成功但文件不可读）: %s" % e,
                          category="screenshot")
    if size <= 0:
        raise BridgeError("截图文件为 0 字节（假成功拦截）: %s" % _sp,
                          category="screenshot")
    data["bytes"] = size


def _capture_warnings_from_data(data: dict) -> List[str]:
    """桥返回体里的需要"喊出来"的信号（UEBox 经验：机位溯源必须透传）。"""
    warns = []
    if data.get("warn_fallback"):
        warns.append("⚠️ camera_source=fallback —— 没找到任何视口/玩家，用的是兜底机位，"
                     "这张图的构图不可据此下结论（UEBox 实测教训）")
    if data.get("note"):
        warns.append(str(data["note"]))
    return warns


def _cmd_capture_viewport(out_path: str = "", camera_mode: str = "active_viewport",
                          pitch: float = 0.0, yaw: float = 0.0, roll: float = 0.0,
                          width: int = 0, height: int = 0) -> dict:
    """截图（T-20261024-UE5BRIDGE-SCREENSHOT）。

    camera_mode: "active_viewport"（当前透视视口机位）| "agent_camera"
    （pitch/yaw/roll 确定性机位 —— 同机位多角度连拍）；PIE 运行中自动转
    FScreenshotRequest 拍游戏画面（pending=true，本函数在非 GT 线程收尾轮询）。
    out_path 只允许约定目录内相对路径（"" = 自动时间戳文件名）。
    width/height: 0 = 视口尺寸（agent_camera 无视口时 1280x720）。
    """
    mode = str(camera_mode or "active_viewport").strip().lower()
    if mode not in ("active_viewport", "agent_camera"):
        raise BridgeError('camera_mode 非法: %r（可选 "active_viewport" | "agent_camera"）'
                          % camera_mode, category="param_type")
    _path = _capture_check_out_path(out_path)
    try:
        _w, _h = max(0, int(width)), max(0, int(height))
    except (TypeError, ValueError):
        raise BridgeError("width/height 必须为整数（0 = 自动）", category="param_type")
    data = _capture_call_bridge_json(
        "capture_viewport",
        (_path, mode, float(pitch), float(yaw), float(roll), _w, _h))
    warnings = _capture_warnings_from_data(data)
    if data.get("pending"):
        if _GT_TASK_RUNNING:
            # 在 GT worker 里：等文件 = 阻塞帧推进 = 死锁。把收尾外包给端点层
            # （execute_command / _mcp_handle_one 在 HTTP 线程调 _capture_finish_pending）。
            return {"success": True, "ok": True, "_pending_poll": data.get("saved_path"),
                    "data": data, "warnings": warnings}
        st = _capture_wait_file(str(data.get("saved_path") or ""))
        data["bytes"] = st["bytes"]
        data.pop("pending", None)
    else:
        _capture_verify_sync_file(data)
    return {"success": True, "ok": True, "data": data, "warnings": warnings}


def _cmd_capture_pie(out_path: str = "", width: int = 0, height: int = 0) -> dict:
    """截 PIE 运行画面（T-20261024-UE5BRIDGE-SCREENSHOT）。

    PIE 未运行 → fail-loud 报错（绝不拿编辑器画面冒充游戏画面）。
    FScreenshotRequest 延迟一帧执行 → pending 轮询（线程规则同 _cmd_capture_viewport）。
    """
    _path = _capture_check_out_path(out_path)
    try:
        _w, _h = max(0, int(width)), max(0, int(height))
    except (TypeError, ValueError):
        raise BridgeError("width/height 必须为整数（0 = 视口尺寸）", category="param_type")
    data = _capture_call_bridge_json("capture_pie", (_path, _w, _h))
    warnings = _capture_warnings_from_data(data)
    if data.get("pending"):
        if _GT_TASK_RUNNING:
            return {"success": True, "ok": True, "_pending_poll": data.get("saved_path"),
                    "_pending_poll_timeout": _CAPTURE_PIE_TIMEOUT_S,
                    "data": data, "warnings": warnings}
        st = _capture_wait_file(str(data.get("saved_path") or ""),
                                timeout_s=_CAPTURE_PIE_TIMEOUT_S)
        data["bytes"] = st["bytes"]
        data.pop("pending", None)
    else:
        _capture_verify_sync_file(data)
    return {"success": True, "ok": True, "data": data, "warnings": warnings}


def _cmd_project_actors_to_screen(actor_names, camera_mode: str = "active_viewport",
                                  pitch: float = 0.0, yaw: float = 0.0,
                                  roll: float = 0.0) -> dict:
    """把 actor 世界坐标投影到与截图同一机位的屏幕像素（标注数据源）。

    actor_names: 名称列表（GetName 或编辑器 Label 均可寻址）。
    返回 projections: [{name,x,y,visible,found,world_location}]；
    视锥外 / 找不到的 actor visible=false（绝不编造坐标）。
    """
    mode = str(camera_mode or "active_viewport").strip().lower()
    if mode not in ("active_viewport", "agent_camera"):
        raise BridgeError('camera_mode 非法: %r（可选 "active_viewport" | "agent_camera"）'
                          % camera_mode, category="param_type")
    if not isinstance(actor_names, (list, tuple)) or not [n for n in actor_names if str(n).strip()]:
        raise BridgeError("actor_names 必须为非空名称列表（先 find_actors 拿名字）",
                          category="param_type")
    names = [str(n).strip() for n in actor_names if str(n).strip()]
    if len(names) > 200:
        raise BridgeError("actor_names 超过上限 200（标注场景用不了这么多）",
                          category="param_type")
    data = _capture_call_bridge_json(
        "project_actors_to_screen", (names, mode, float(pitch), float(yaw), float(roll)))
    warnings = _capture_warnings_from_data(data)
    return {"success": True, "ok": True, "data": data, "warnings": warnings}


# ── 截图编码与标注（ue5_screenshot MCP 工具的宿主侧管线）──────────────

def _load_pillow():
    """Pillow 可选依赖装载（任务规格：import 失败 → 跳过标注/压缩，绝不失败）。

    独立函数 = 测试注入缝（monkeypatch 本函数可模拟有/无 Pillow 两种环境）。
    返回 {"Image":..., "ImageDraw":..., "ImageFont":...} 或 None。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415
        return {"Image": Image, "ImageDraw": ImageDraw, "ImageFont": ImageFont}
    except Exception:
        return None


def _png_dimensions(raw: bytes):
    """纯 stdlib 解析 PNG 尺寸（IHDR）；非 PNG / 损坏返回 None（fail-loud 由调用方做）。"""
    try:
        import struct
        if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
            return None
        w, h = struct.unpack(">II", raw[16:24])
        return int(w), int(h)
    except Exception:
        return None


def _pil_load_and_verify(path: str, pil):
    """读回验证（存在 + 非空 + 可解码 + 尺寸），返回 (PIL.Image, w, h)。

    有 Pillow → 真解码；无 Pillow → PNG 魔数 + IHDR 结构解析（不够"真解码"
    但能锁住尺寸与文件完整性，本机/最小环境照样可用）。
    任一不符 → BridgeError（fail-loud：绝不把坏文件当截图返回）。
    """
    if not os.path.isfile(path):
        raise BridgeError("截图文件不存在: %s" % path, category="screenshot")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise BridgeError("截图文件读取失败: %s" % e, category="screenshot")
    if not raw:
        raise BridgeError("截图文件为空（0 字节）: %s" % path, category="screenshot")
    if pil is not None:
        try:
            img = pil["Image"].open(path)
            img.load()
            return img, int(img.width), int(img.height)
        except Exception as e:
            raise BridgeError("截图文件不可解码: %s（%s）" % (path, e), category="screenshot")
    dims = _png_dimensions(raw)
    if dims is None:
        raise BridgeError("截图文件不是可识别的 PNG（且 Pillow 未安装，无法解码其他格式）: %s"
                          % path, category="screenshot")
    return None, dims[0], dims[1]


def _pil_cjk_font(pil, size: int = 14):
    """标注字体：中文优先（微软雅黑/黑体），逐级 fallback 到默认位图字体。

    任何一步失败都只降级不失败（标注是锦上添花，字体缺失不能弄挂截图）。
    """
    ImageFont = pil["ImageFont"]
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",   # 微软雅黑（含中文）
        r"C:\Windows\Fonts\simhei.ttf",  # 黑体（含中文）
        r"C:\Windows\Fonts\arial.ttf",   # Arial（仅西文兜底）
    ]
    for fp in candidates:
        try:
            if os.path.isfile(fp):
                return ImageFont.truetype(fp, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _pil_annotate(img, projections, pil):
    """在原图上画 actor 名字标签（圆角底 + 文字 + 定位十字）。

    只画 visible=true 的投影（视锥外/找不到的不画 —— 规格书：不编造）。
    返回 (annotated_names, notes)；坐标无效的条目跳过并记 note（不失败）。
    """
    draw = pil["ImageDraw"].Draw(img)
    font = _pil_cjk_font(pil)
    names, notes = [], []
    for p in projections:
        if not isinstance(p, dict) or not p.get("visible"):
            continue
        try:
            x, y = int(float(p.get("x"))), int(float(p.get("y")))
        except (TypeError, ValueError):
            notes.append("投影 %r 坐标无效，未标注" % (p.get("name"),))
            continue
        name = str(p.get("name") or "?")
        # 定位十字（10px）
        draw.line([(x - 5, y), (x + 5, y)], fill=(255, 90, 40), width=2)
        draw.line([(x, y - 5), (x, y + 5)], fill=(255, 90, 40), width=2)
        # 标签底板（文字左上角在十字右上侧）
        lx, ly = x + 8, y - 22
        if font is not None:
            try:
                bbox = draw.textbbox((0, 0), name, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            except Exception:
                tw, th = len(name) * 7, 14
        else:
            tw, th = len(name) * 7, 14
        pad = 3
        box = (lx, ly, lx + tw + pad * 2, ly + th + pad * 2)
        # rounded_rectangle 需 Pillow ≥ 8.2 —— 旧版降级普通矩形（不因版本失败）
        if hasattr(draw, "rounded_rectangle"):
            draw.rounded_rectangle(box, radius=3, fill=(20, 20, 20), outline=(255, 90, 40))
        else:
            draw.rectangle(box, fill=(20, 20, 20), outline=(255, 90, 40))
        draw.text((lx + pad, ly + pad), name, fill=(255, 255, 255), font=font)
        names.append(name)
    return names, notes


def _pil_compress_jpeg(img, max_dimension: int, pil):
    """长边压缩到 max_dimension + JPEG q80 → (jpeg_bytes, out_w, out_h)。

    短边等比缩放；LANCZOS 失败降级 BILINEAR（老 Pillow 的枚举差异不弄挂工具）。
    """
    Image = pil["Image"]
    w, h = img.width, img.height
    long_edge = max(w, h)
    if long_edge > max_dimension:
        ratio = max_dimension / float(long_edge)
        nw, nh = max(1, int(round(w * ratio))), max(1, int(round(h * ratio)))
        try:
            resample = Image.LANCZOS
        except AttributeError:  # Pillow 老版本枚举名差异
            resample = Image.BILINEAR
        img = img.convert("RGB").resize((nw, nh), resample)
    else:
        img = img.convert("RGB")
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue(), img.width, img.height


def _value_matches(requested, readback):
    """回读比对（v3.0.1）：精确 → 数值容差 → 去引号 → 数字 token 序列。

    覆盖三类真实差异：浮点显示差（0.35 vs 0.3499999940395355）、
    结构体导出格式差（(R=1.0,...) vs (R=1.000000,...)）、引号/空白差。
    返回 True = 视为一致；False = 不一致（调用方 fail-loud）。
    """
    if requested is None or readback is None:
        return False
    rs, gs = str(requested).strip(), str(readback).strip()
    if rs == gs:
        return True
    try:
        rf, gf = float(rs), float(gs)
        return abs(rf - gf) <= max(1e-4, abs(rf) * 1e-4)
    except (TypeError, ValueError):
        pass
    if rs.strip('"') == gs.strip('"'):
        return True
    import re as _re
    rn = [float(x) for x in _re.findall(r"-?\d+\.?\d*", rs)]
    gn = [float(x) for x in _re.findall(r"-?\d+\.?\d*", gs)]
    if rn and gn and len(rn) == len(gn):
        return all(abs(a - b) <= max(1e-4, abs(a) * 1e-4) for a, b in zip(rn, gn))
    return False


# ── ⑤ 材质（v3.0 · T-20260918-V3-MAT）────────────────────
#  探针实测（UE 5.1.1 · 2026-09-18）：unreal.MaterialEditingLibrary 已完整暴露
#  （72 方法含 create_material_expression / connect_material_expressions /
#   connect_material_property / recompile_material / layout_material_expressions），
#  unreal.MaterialFactoryNew 与 MaterialInstanceConstantFactoryNew 可用
#  → **纯 Python 路线，零 C++ 改动**。
#  表达式跨请求引用问题：以「表达式名」（如 MaterialExpressionScalarParameter_0）
#  作为稳定锚点（与节点 GUID 定位同思路），每次请求内按名回查。

_MAT_PROP_MAP = {
    "base_color": "MP_BASE_COLOR", "basecolor": "MP_BASE_COLOR",
    "metallic": "MP_METALLIC", "specular": "MP_SPECULAR",
    "roughness": "MP_ROUGHNESS",
    "emissive_color": "MP_EMISSIVE_COLOR", "emissive": "MP_EMISSIVE_COLOR",
    "opacity": "MP_OPACITY", "opacity_mask": "MP_OPACITY_MASK",
    "normal": "MP_NORMAL", "world_position_offset": "MP_WORLD_POSITION_OFFSET",
    "ambient_occlusion": "MP_AMBIENT_OCCLUSION", "ao": "MP_AMBIENT_OCCLUSION",
    "refraction": "MP_REFRACTION", "displacement": "MP_DISPLACEMENT",
}


def _mat_prop_enum(name):
    u = unreal
    key = _MAT_PROP_MAP.get(str(name or "").strip().lower())
    if not key:
        raise BridgeError(
            "未知材质属性: %r" % name, category="material",
            suggestion="可用: " + "/".join(sorted(set(_MAT_PROP_MAP.keys()))))
    mp = getattr(u, "MaterialProperty", None)
    v = getattr(mp, key, None) if mp is not None else None
    if v is None:
        raise BridgeError("unreal.MaterialProperty.%s 不可用（引擎枚举缺失）" % key,
                          category="material")
    return v


def _mat_me():
    u = unreal
    mel = getattr(u, "MaterialEditingLibrary", None)
    if mel is None:
        raise BridgeError("unreal.MaterialEditingLibrary 不可用（本引擎未暴露材质编辑库）",
                          category="material")
    return mel


def _mat_load(mat_path: str):
    pkg = _safe_game_pkg(mat_path)
    if not pkg:
        raise BridgeError("材质路径非法（只允许规范 /Game/ 路径）: %r"
                          % mat_path, category="param_type")
    mat = unreal.EditorAssetLibrary.load_asset(pkg)
    if not mat:
        raise BridgeError("材质加载失败: %s" % mat_path, category="material")
    return mat


def _mat_expressions(mat):
    """读取材质表达式列表（候选链：C++ 桥 GetMaterialExpressions → Python 属性）。

    实测（UE5.1 · 2026-09-18）：`mat.get_editor_property("expressions")` 不可读
    （受保护成员）、MaterialEditingLibrary 只有 get_num_material_expressions
    → 表达式枚举必须走 v3.0 桥 API；老 DLL 上退化为可读错误（fail-loud）。
    """
    if BRIDGE_AVAILABLE and hasattr(bridge, "get_material_expressions"):
        try:
            exprs = bridge.get_material_expressions(mat)
            if exprs is not None:
                return list(exprs)
        except Exception:
            pass
    try:
        exprs = mat.get_editor_property("expressions")
        if exprs is not None:
            return list(exprs)
    except Exception:
        pass
    mel = _mat_me()
    fn = getattr(mel, "get_material_expressions", None)
    if callable(fn):
        try:
            return list(fn(mat) or [])
        except Exception:
            pass
    raise BridgeError(
        "无法枚举材质表达式（expressions 属性与 get_material_expressions 均不可用）",
        category="material")


def _mat_find_expression(mat, name: str):
    for e in _mat_expressions(mat):
        try:
            if str(e.get_name()) == name:
                return e
        except Exception:
            continue
    raise BridgeError("表达式未找到: %s（先用 material_read 查表达式名）" % name,
                      category="material")


# ══════════════════════════════════════════════════════════
#  材质写操作回读（v3.3.1）
#  动机：蓝图侧已证实「引擎 API 的 bool 返回值会说谎」（P01/P04：ConnectPins
#  是可行性验证 API，类型不匹配时建隐式 Cast 节点仍返回 True）。材质侧
#  connect_material_expressions / connect_material_property 是同类 API，
#  原先只判返回值 → 同一类假阳性没有防护。
#  降级口径对齐 `_pywrap_readback_nodes` 的 R1 策略：**能读到就严格比对，
#  读不到只记 warning 不阻断**（避免非 GameThread 等场景制造假阴性）。
# ══════════════════════════════════════════════════════════

def _mat_expression_names(mat):
    """表达式名集合（快照/回读用）。失败返回 None = 回读通道不可用。"""
    try:
        return {str(e.get_name()) for e in _mat_expressions(mat)}
    except Exception:
        return None


def _mat_prop_attr(property_name: str):
    """友好属性名 → UMaterial 上的 UPROPERTY 名（MP_BASE_COLOR → base_color）。"""
    key = _MAT_PROP_MAP.get(str(property_name or "").strip().lower())
    if not key:
        return None
    return key[3:].lower() if key.startswith("MP_") else None


def _mat_linked_expr_name(container, input_name: str, label: str = ""):
    """回读 container 上某输入的连接目标 → (expr_name|'', err)。

    container 可为 UMaterialExpression（输入属性如 A/B/Coordinates）或
    UMaterial（主属性如 base_color）。
    err 非空 = 回读通道不可用（调用方记 warning）；err 为空且返回 '' = 确实未连线。
    """
    if not input_name:
        return "", "输入名为空，无法定位回读目标"
    try:
        pin = container.get_editor_property(input_name)
    except Exception as e:
        return "", "输入 %r 不可读: %s" % (input_name, e)
    linked = getattr(pin, "expression", None)
    if linked is None:
        # 部分版本用 get_expression() 取
        getter = getattr(pin, "get_expression", None)
        if callable(getter):
            try:
                linked = getter()
            except Exception:
                linked = None
    if linked is None:
        return "", ""
    try:
        return str(linked.get_name()), ""
    except Exception as e:
        return "", "连接目标不可读: %s" % e


def _mat_first_input_name(mat, expr):
    """取表达式第一个输入名（connect_* 的「空串 = 默认第一引脚」口径）。"""
    mel = _mat_me()
    fn = getattr(mel, "get_inputs_for_material_expression", None)
    if not callable(fn):
        return None, "get_inputs_for_material_expression 不可用"
    try:
        names = [str(x) for x in (fn(mat, expr) or [])]
    except Exception as e:
        return None, "输入名枚举失败: %s" % e
    if not names:
        return None, "该表达式无输入引脚"
    return names[0], ""


def _cmd_material_create(material_path: str, package_path: str = "") -> dict:
    """创建材质资产（UMaterial + MaterialFactoryNew）。"""
    _pywrap_require_str("material_path", material_path)
    path = str(material_path).strip()
    if not _safe_game_pkg(path):
        raise BridgeError("material_path 需为规范 /Game/... 包路径（禁止 ..）",
                          category="param_type")
    pkg = package_path.strip() or path.rsplit("/", 1)[0]
    name = path.rsplit("/", 1)[-1].split(".")[0]

    def _do():
        u = unreal
        full = path.split(".")[0]
        if u.EditorAssetLibrary.does_asset_exist(full):
            raise BridgeError("材质已存在: %s" % full, category="material")
        factory = u.MaterialFactoryNew()
        tools = u.AssetToolsHelpers.get_asset_tools()
        mat = tools.create_asset(name, pkg, u.Material, factory)
        if not mat:
            raise BridgeError("create_asset 返回 None（材质未创建）", category="material")
        return {"material": str(mat.get_path_name())}

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    if _TASK_JOURNAL_AVAILABLE and _task_journal is not None:
        try:
            _task_journal.note_created_asset(path.split(".")[0])
        except Exception:
            pass
    return {"success": True, "ok": True, "data": out}


def _cmd_material_add_expression(material_path: str, expression_class: str,
                                 node_pos=None) -> dict:
    """添加材质表达式（如 ScalarParameter / VectorParameter / Multiply / TextureSample）。

    expression_class: 短名（"ScalarParameter"）或全路径。
    返回表达式名（后续 connect/set 的稳定锚点）。
    """
    _pywrap_require_str("expression_class", expression_class)
    pos = list(node_pos or [0, 0])
    if len(pos) != 2:
        raise BridgeError("node_pos 必须是 [x, y]", category="param_type")
    cls_raw = str(expression_class).strip()
    if not cls_raw.startswith("/"):
        cls_raw = "/Script/Engine.MaterialExpression" + cls_raw

    def _do():
        u = unreal
        mat = _mat_load(material_path)
        cls = u.load_class(None, cls_raw)
        if cls is None and not str(expression_class).startswith("/"):
            cls = u.load_class(None, "/Script/Engine." + str(expression_class).strip())
        if cls is None:
            raise BridgeError(
                "表达式类不可解析: %s（示例 /Script/Engine.MaterialExpressionScalarParameter）"
                % cls_raw, category="material")
        mel = _mat_me()
        before = _mat_expression_names(mat)
        expr = mel.create_material_expression(mat, cls, int(pos[0]), int(pos[1]))
        if expr is None:
            raise BridgeError("create_material_expression 返回 None（表达式未创建）",
                              category="material")
        name = str(expr.get_name())
        # ── 回读：确认表达式真的落进了材质（不能只信返回对象非 None）──
        warnings = []
        after = _mat_expression_names(mat)
        if before is None or after is None:
            warnings.append(
                "表达式回读不可用（_mat_expressions 失败）→ 未能确认 %s 已落图" % name)
        elif name not in after:
            raise BridgeError(
                "表达式回读不一致：%s 未出现在材质表达式列表中（API 返回了对象但未落图）"
                % name, category="material")
        return {"name": name, "class": str(cls.get_path_name()),
                "warnings": warnings}

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": out}


def _cmd_material_connect_expressions(material_path: str, from_name: str,
                                      to_name: str, from_output: str = "",
                                      to_input: str = "") -> dict:
    """连接两个表达式（from 输出 → to 输入）。"""
    _pywrap_require_str("from_name", from_name)
    _pywrap_require_str("to_name", to_name)

    def _do():
        mat = _mat_load(material_path)
        mel = _mat_me()
        src = _mat_find_expression(mat, from_name)
        dst = _mat_find_expression(mat, to_name)
        before = _mat_expression_names(mat)
        ok = mel.connect_material_expressions(
            src, str(from_output or ""), dst, str(to_input or ""))
        if not ok:
            raise BridgeError(
                "connect_material_expressions 返回 False（%s.%s → %s.%s）—— "
                "检查引脚名（空串=默认第一引脚）与类型兼容性"
                % (from_name, from_output or "<default>", to_name, to_input or "<default>"),
                category="material")
        # ── 回读比对（P04 教训平移：材质侧同类 API 的 bool 同样会说谎）──
        # verified 与 warnings 解耦：verified 只回答「回读比对是否真的执行且通过」，
        #   不因非致命告警（如检测到隐式转换节点）而变 False —— 否则连线已验证通过
        #   却报 verified=False，等于把两个概念混在一个字段里（本次审计自查修正）。
        warnings = []
        verified = False
        # ① 隐式转换节点检测：类型不匹配时引擎会插入转换表达式 —— 蓝图侧
        #    `TryCreateConnection` 正是这样"返回 True 但原 Pin 无 LinkedTo"。
        #    注意：本项只是线索上报，**不影响 verified**（连线本身由 ② 判定）。
        after = _mat_expression_names(mat)
        if before is not None and after is not None:
            added = sorted(after - before)
            if added:
                warnings.append(
                    "连线后新增 %d 个表达式（可能是引擎插入的隐式类型转换节点）: %s"
                    % (len(added), added))
        # ② 输入引脚连接目标比对
        input_name = str(to_input or "")
        if not input_name:
            input_name, in_err = _mat_first_input_name(mat, dst)
            if in_err:
                warnings.append("输入名不可解析（%s）→ 未做连接回读比对" % in_err)
                input_name = ""
        if input_name:
            linked, rb_err = _mat_linked_expr_name(
                dst, input_name, label="%s.%s" % (to_name, input_name))
            if rb_err:
                warnings.append("连线回读不可用（%s）→ 未做比对" % rb_err)
            elif linked != from_name:
                raise BridgeError(
                    "连线回读不一致：%s.%s 实际连到 %r，期望 %r"
                    "（API 返回 True 但未生效）"
                    % (to_name, input_name, linked or "<未连线>", from_name),
                    category="material")
            else:
                verified = True
        return {"connected": True, "from": from_name, "to": to_name,
                "verified": verified, "warnings": warnings}

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": out}


def _cmd_material_connect_property(material_path: str, from_name: str,
                                   property_name: str, from_output: str = "") -> dict:
    """把表达式输出连到材质主属性（base_color/metallic/roughness/...）。"""
    _pywrap_require_str("from_name", from_name)
    _pywrap_require_str("property_name", property_name)
    _enum = _mat_prop_enum(property_name)

    def _do():
        mat = _mat_load(material_path)
        mel = _mat_me()
        src = _mat_find_expression(mat, from_name)
        ok = mel.connect_material_property(src, str(from_output or ""), _enum)
        if not ok:
            raise BridgeError("connect_material_property 返回 False（%s → %s）"
                              % (from_name, property_name), category="material")
        # ── 回读比对：材质主属性当前连到的表达式 ──
        # verified 与 warnings 解耦（同 connect_expressions 口径）。
        warnings = []
        verified = False
        attr = _mat_prop_attr(property_name)
        if not attr:
            warnings.append("属性名 %r 无法映射到 UPROPERTY → 未做连接回读比对"
                            % property_name)
        else:
            linked, rb_err = _mat_linked_expr_name(
                mat, attr, label="material.%s" % attr)
            if rb_err:
                warnings.append("连线回读不可用（%s）→ 未做比对" % rb_err)
            elif linked != from_name:
                raise BridgeError(
                    "连线回读不一致：材质属性 %s 实际连到 %r，期望 %r"
                    "（API 返回 True 但未生效）"
                    % (property_name, linked or "<未连线>", from_name),
                    category="material")
            else:
                verified = True
        return {"connected": True, "from": from_name, "property": property_name,
                "verified": verified, "warnings": warnings}

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": out}


def _cmd_material_set_expression_property(material_path: str, expr_name: str,
                                          property_name: str, value=None) -> dict:
    """设置表达式属性（ScalarParameter.default_value / parameter_name / VectorParameter.default_value 等）。

    值类型自适应（bool/int/float/list→LinearColor|Vector/str）；设置后回读回传（可审计）。
    """
    _pywrap_require_str("expr_name", expr_name)
    _pywrap_require_str("property_name", property_name)

    def _do():
        u = unreal
        mat = _mat_load(material_path)
        expr = _mat_find_expression(mat, expr_name)
        prop = str(property_name)
        v = value
        try:
            cur = expr.get_editor_property(prop)
            cur_t = type(cur).__name__
            if isinstance(value, bool):
                v = bool(value)
            elif isinstance(value, (int, float)) and cur_t.lower() in ("float", "double"):
                v = float(value)
            elif isinstance(value, str):
                if cur_t in ("float", "double"):
                    v = float(value)
                elif cur_t == "int":
                    v = int(float(value))
                elif cur_t == "bool":
                    v = value.strip().lower() in ("1", "true", "yes", "on")
                elif cur_t == "LinearColor":
                    parts = value.replace(",", " ").split()
                    v = u.LinearColor(*[float(x) for x in parts[:4]])
                elif cur_t == "Vector":
                    parts = value.replace(",", " ").split()
                    v = u.Vector(*[float(x) for x in parts[:3]])
                else:
                    v = value
            elif isinstance(value, (list, tuple)):
                if cur_t == "LinearColor":
                    v = u.LinearColor(*[float(x) for x in list(value)[:4]])
                elif cur_t == "Vector":
                    v = u.Vector(*[float(x) for x in list(value)[:3]])
        except Exception as e:
            raise BridgeError("属性 %r 读取失败（无法确定目标类型）: %s" % (prop, e),
                              category="material")
        try:
            expr.set_editor_property(prop, v)
        except Exception as e:
            raise BridgeError("set_editor_property(%s, %r) 失败: %s" % (prop, v, e),
                              category="material")
        got = None
        try:
            got = expr.get_editor_property(prop)
        except Exception:
            pass
        # v3.0.1（取舍#6）：回读比对（数值容差/结构体 token 序列），未生效即 fail-loud
        if got is None:
            raise BridgeError(
                "设置后回读不可用（无法验证是否生效）: %s.%s" % (expr_name, prop),
                category="material")
        _req_str = _component_property_value_to_string(v)
        _got_str = _component_property_value_to_string(got)
        if not _value_matches(_req_str, _got_str):
            raise BridgeError(
                "设置未生效（回读比对失败）: %s.%s 请求=%r 回读=%r"
                % (expr_name, prop, _req_str, _got_str),
                category="material")
        return {"expr": expr_name, "property": prop, "requested": _safe_json(v),
                "readback": _safe_json(got), "readback_type": type(got).__name__,
                "verified": True}

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": out}


def _cmd_material_compile(material_path: str) -> dict:
    """重编译材质 + 回读表达式计数（recompile_material 返回 False → fail-loud）。"""
    def _do():
        mat = _mat_load(material_path)
        mel = _mat_me()
        ok = mel.recompile_material(mat)
        # 口径与全项目 fail-loud 一致用 `not ok`：`is False` 会放过 None
        #   （引擎 API 失败路径返回 None 是常态）→ 会把未编译报成 compiled:True。
        if not ok:
            raise BridgeError(
                "recompile_material 未返回 True（实收 %r）——材质编译失败，检查连接/表达式"
                % (ok,),
                category="material")
        expr_count = None
        try:
            expr_count = len(_mat_expressions(mat))
        except Exception:
            pass
        return {"compiled": True, "expression_count": expr_count}

    out = _run_on_game_thread_sync(_do, timeout=60.0)
    return {"success": True, "ok": True, "data": out}


def _cmd_material_read(material_path: str) -> dict:
    """读取材质表达式清单（名称/类/位置）。"""
    def _do():
        mat = _mat_load(material_path)
        out = []
        for e in _mat_expressions(mat):
            item = {"name": "", "class": ""}
            try:
                item["name"] = str(e.get_name())
            except Exception:
                pass
            try:
                item["class"] = str(e.get_class().get_name())
            except Exception:
                pass
            try:
                item["pos"] = [int(e.get_editor_property("material_expression_editor_x")),
                               int(e.get_editor_property("material_expression_editor_y"))]
            except Exception:
                pass
            out.append(item)
        return {"material": material_path, "count": len(out), "expressions": out}

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": out}


def _cmd_material_instance_create(material_path: str, instance_path: str) -> dict:
    """创建材质实例（父材质 = material_path）。"""
    _pywrap_require_str("instance_path", instance_path)

    def _do():
        u = unreal
        full = str(instance_path).split(".")[0]
        if u.EditorAssetLibrary.does_asset_exist(full):
            raise BridgeError("材质实例已存在: %s" % full, category="material")
        parent = _mat_load(material_path)
        pkg = full.rsplit("/", 1)[0]
        name = full.rsplit("/", 1)[-1]
        factory = u.MaterialInstanceConstantFactoryNew()
        tools = u.AssetToolsHelpers.get_asset_tools()
        mi = tools.create_asset(name, pkg, u.MaterialInstanceConstant, factory)
        if not mi:
            raise BridgeError("材质实例创建失败（create_asset 返回 None）", category="material")
        u.MaterialEditingLibrary.set_material_instance_parent(mi, parent)
        return {"instance": str(mi.get_path_name()), "parent": material_path}

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": out}


def _cmd_material_instance_set_parameter(instance_path: str, parameter_name: str,
                                         kind: str = "scalar", value=None) -> dict:
    """设置材质实例参数（kind: scalar | vector | texture）。"""
    _pywrap_require_str("parameter_name", parameter_name)
    _k = str(kind or "scalar").strip().lower()
    if _k not in ("scalar", "vector", "texture"):
        raise BridgeError('kind 非法: %r（scalar | vector | texture）' % kind,
                          category="param_type")

    def _do():
        u = unreal
        mi = u.EditorAssetLibrary.load_asset(instance_path)
        if not mi:
            raise BridgeError("材质实例加载失败: %s" % instance_path, category="material")
        mel = _mat_me()
        pname = str(parameter_name)
        if _k == "scalar":
            mel.set_material_instance_scalar_parameter_value(mi, pname, float(value or 0.0))
        elif _k == "vector":
            if isinstance(value, (list, tuple)):
                vals = [float(x) for x in list(value)]
            else:
                vals = [float(x) for x in str(value).replace(",", " ").split()]
            vals = list(vals) + [1.0] * (4 - len(vals))
            mel.set_material_instance_vector_parameter_value(
                mi, pname, u.LinearColor(*vals[:4]))
        else:
            tex = u.EditorAssetLibrary.load_asset(str(value))
            if not tex:
                raise BridgeError("纹理加载失败: %s" % value, category="material")
            mel.set_material_instance_texture_parameter_value(mi, pname, tex)
        return {"instance": instance_path, "parameter": pname, "kind": _k}

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": out}


# ── ⑥ UMG 控件蓝图（v3.0 · T-20260918-V3-UMG）────────────
#  C++ 扩展（CreateWidgetBlueprint / AddWidgetToTree / SetWidgetProperty /
#  ReadWidgetTree）由 v3.0 版 bridge DLL 提供；旧 DLL 上调用 → fail-loud
#  提示重编译（绝不给"看起来成功"的假象）。节点级图操作（建事件/函数节点、
#  连线、改参、编译）对 WidgetBlueprint 的 EventGraph 复用既有 L1/L2 命令。

def _wrapb_require():
    if not BRIDGE_AVAILABLE:
        raise BridgeError("BlueprintPythonBridge 未加载", category="widget")
    missing = [n for n in ("create_widget_blueprint", "add_widget_to_tree",
                           "set_widget_property", "read_widget_tree",
                           "read_widget_property")
               if not hasattr(bridge, n)]
    if missing:
        raise BridgeError(
            "当前 bridge DLL 无 UMG API（缺: %s）—— 需 v3.0 版 DLL"
            % ", ".join(missing),
            category="widget",
            suggestion="用随包 Source/build_for_engine.bat 重编译并部署后重启编辑器")
    return bridge


def _wrapb_resolve(widget_path: str):
    """加载控件蓝图（校验是 UWidgetBlueprint）。"""
    wp = unreal.EditorAssetLibrary.load_asset(widget_path)
    if not wp:
        raise BridgeError("控件蓝图加载失败: %s" % widget_path, category="widget")
    return wp


def _wrapb_norm_path(widget_path: str) -> str:
    pkg = _safe_game_pkg(widget_path)
    if not pkg:
        raise BridgeError("widget_path 需为规范 /Game/... 包路径（禁止 ..）",
                          category="param_type")
    return pkg


def _cmd_widget_create(widget_path: str, package_path: str = "") -> dict:
    """创建 UMG 控件蓝图（父类 UUserWidget；自动建 WidgetTree + 编译）。"""
    _pywrap_require_str("widget_path", widget_path)
    b = _wrapb_require()
    full = _wrapb_norm_path(widget_path)
    if not _safe_game_pkg(full):
        raise BridgeError("widget_path 需为规范 /Game/... 包路径（禁止 ..）",
                          category="param_type")
    pkg = package_path.strip() or full.rsplit("/", 1)[0]
    name = full.rsplit("/", 1)[-1]

    def _do():
        if unreal.EditorAssetLibrary.does_asset_exist(full):
            raise BridgeError("控件蓝图已存在: %s" % full, category="widget")
        wp = b.create_widget_blueprint(pkg, name)
        if wp is None:
            raise BridgeError("create_widget_blueprint 返回 None（未创建）",
                              category="widget")
        return {"widget": str(wp.get_path_name())}

    out = _run_on_game_thread_sync(_do, timeout=60.0)
    if _TASK_JOURNAL_AVAILABLE and _task_journal is not None:
        try:
            _task_journal.note_created_asset(full)
        except Exception:
            pass
    return {"success": True, "ok": True, "data": out}


def _cmd_widget_add_child(widget_path: str, widget_class: str,
                          widget_name: str, parent_name: str = "") -> dict:
    """向控件树添加控件（parent_name 空 = 设为根；父须为 Panel 类控件如 CanvasPanel）。"""
    _pywrap_require_str("widget_class", widget_class)
    _pywrap_require_str("widget_name", widget_name)
    b = _wrapb_require()
    full = _wrapb_norm_path(widget_path)

    def _do():
        wp = _wrapb_resolve(full)
        err = b.add_widget_to_tree(wp, str(parent_name or ""), str(widget_class),
                                   str(widget_name))
        # 反射语义（裁决 A）：'' = 成功 / 非空 = 失败文本 / None = 失败无文本
        if err is None:
            raise BridgeError("add_widget_to_tree 返回 None（失败且无错误文本）",
                              category="widget")
        if isinstance(err, str) and err:
            raise BridgeError("add_widget_to_tree 失败: %s" % err, category="widget")
        # ── 回读比对：确认控件真的进了树（同族 widget_set_property 已有
        #    ReadWidgetProperty 回读，此处原先缺失 → 族内验证密度不一致）──
        # verified 与 warnings 解耦（同材质族口径）。
        warnings = []
        verified = False
        try:
            infos = b.read_widget_tree(wp) or []
        except Exception as e:
            infos = None
            warnings.append("控件树回读不可用（read_widget_tree 异常: %s）→ 未做比对" % e)
        if infos is not None:
            names = set()
            for i in infos:
                for k in ("widget_name", "WidgetName"):
                    v = getattr(i, k, None)
                    if v is not None:
                        names.add(str(v))
                        break
            if str(widget_name) not in names:
                raise BridgeError(
                    "控件树回读不一致：%s 未出现在控件树中（API 报成功但未落树）"
                    % widget_name, category="widget")
            verified = True
        return {"widget": full, "name": str(widget_name),
                "class": str(widget_class), "parent": str(parent_name or "<root>"),
                "verified": verified, "warnings": warnings}

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": out}


def _cmd_widget_set_property(widget_path: str, widget_name: str,
                             property_name: str, value: str = "") -> dict:
    """设置控件属性（FindFProperty + ImportText_Direct；值用文本格式，如 Text="Hello" / "1.0"）。"""
    _pywrap_require_str("widget_name", widget_name)
    _pywrap_require_str("property_name", property_name)
    b = _wrapb_require()
    full = _wrapb_norm_path(widget_path)

    def _do():
        wp = _wrapb_resolve(full)
        _val = str(value if value is not None else "")
        err = b.set_widget_property(wp, str(widget_name), str(property_name), _val)
        if err is None:
            raise BridgeError("set_widget_property 返回 None（失败且无错误文本）",
                              category="widget")
        if isinstance(err, str) and err:
            raise BridgeError("set_widget_property 失败: %s" % err, category="widget")
        # v3.0.1（取舍#6）：设置后**回读比对**，未生效即 fail-loud（绝不假成功）
        #  v3.0.1a 自适应：FSlateColor 类属性（ColorAndOpacity 等）ImportText 期望
        #  (SpecifiedColor=(R=..,G=..,B=..,A=..))，而美术习惯写 (R=..,G=..,B=..,A=..)
        #  —— 后者会被 ImportText **静默接受但值不变**（实测假成功，2026-09-18）。
        #  回读发现回读文本含 SpecifiedColor= 且请求未包装时 → 自动重试包装格式。
        def _apply(_v):
            _e = b.set_widget_property(wp, str(widget_name), str(property_name), _v)
            if _e is None:
                raise BridgeError("set_widget_property 返回 None（失败且无错误文本）",
                                  category="widget")
            if isinstance(_e, str) and _e:
                raise BridgeError("set_widget_property 失败: %s" % _e, category="widget")

        def _readback():
            _raw = b.read_widget_property(wp, str(widget_name), str(property_name))
            if isinstance(_raw, str) and _raw.startswith("ERROR:"):
                raise BridgeError("回读失败: %s" % _raw[6:].strip(), category="widget")
            return "" if _raw is None else str(_raw)

        _applied_value = _val
        _back = None
        _note = ""
        if hasattr(b, "read_widget_property"):
            _back = _readback()
            if not _value_matches(_val, _back):
                _vs = _val.strip()
                if (_back and "SpecifiedColor=" in _back
                        and _vs.startswith("(") and "SpecifiedColor" not in _vs):
                    _wrapped = "(SpecifiedColor=%s)" % _vs
                    _apply(_wrapped)
                    _back2 = _readback()
                    if _value_matches(_val, _back2):
                        _applied_value = _wrapped
                        _back = _back2
                        _note = ("FSlateColor 包装格式自适应：已按 (SpecifiedColor=...) "
                                 "重试并回读通过")
                    else:
                        raise BridgeError(
                            "设置未生效（回读比对失败，含包装格式重试）: %s.%s "
                            "请求=%r 原回读=%r 包装重试回读=%r"
                            % (str(widget_name), str(property_name), _val, _back, _back2),
                            category="widget")
                else:
                    raise BridgeError(
                        "设置未生效（回读比对失败）: %s.%s 请求=%r 回读=%r"
                        % (str(widget_name), str(property_name), _val, _back),
                        category="widget")
        _out = {"widget": full, "target": str(widget_name),
                "property": str(property_name), "value": _val,
                "applied_value": _applied_value,
                "readback": _back, "verified": _back is not None}
        if _note:
            _out["note"] = _note
        return _out

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": out}


def _cmd_widget_read_tree(widget_path: str) -> dict:
    """读取控件树（名称 / 类路径 / 父控件名）。"""
    b = _wrapb_require()
    full = _wrapb_norm_path(widget_path)

    def _do():
        wp = _wrapb_resolve(full)
        infos = b.read_widget_tree(wp) or []
        items = []
        for i in infos:
            item = {}
            for k in ("widget_name", "WidgetName"):
                v = getattr(i, k, None)
                if v is not None:
                    item["name"] = str(v)
                    break
            for k in ("widget_class", "WidgetClass"):
                v = getattr(i, k, None)
                if v is not None:
                    item["class"] = str(v)
                    break
            for k in ("parent_name", "ParentName"):
                v = getattr(i, k, None)
                if v is not None:
                    item["parent"] = str(v)
                    break
            items.append(item)
        return {"widget": full, "count": len(items), "widgets": items}

    out = _run_on_game_thread_sync(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": out}


# ══════════════════════════════════════════════════════════
#  T-20261024-UE5BRIDGE-GRAPH-APPLY · 整图声明式写入
#  （search_blueprint_nodes 只读查签名 + apply_graph 声明式整图写入）
#  ── 设计原则（规格书 P1-整图声明式写入设计.md · UEBox 透析）──
#  · UEBox 删除了全部逐节点写图工具，第一条规则是 "Never build a graph one
#    node at a time" —— 逐节点改图是上下文与轮次黑洞。本库**保留**逐节点手术式
#    编辑（fail-loud 纪律的用武之地），另增「查一批 → 写一整张」主路径。
#  · 复用既有机制（不重造）：apply_graph = build_blueprint 的原子事务 +
#    connect_pins 的拓扑验证 + 读取族的读回验证纪律，组合成一个原子操作。
#  · 整图写入是替换语义 + 明确守卫（409/非空拒绝），不是静默合并。
#  · 编译在事务外（UEBox 纪律）；编译中的蓝图拒绝写入。
#  · 自动布局用确定性纯 Python 算法（拓扑序网格），**不引 elkjs**
#    （EPL-2.0 许可边界，见规格书）。
# ══════════════════════════════════════════════════════════

_GRAPH_APPLY_TASK = "T-20261024-UE5BRIDGE-GRAPH-APPLY"

#: search 的 limit 上限（规格书：防响应洪水；超过 → 收敛 + warning，不报错）
_GRAPH_SEARCH_LIMIT_MAX = 50

#: 引擎函数库候选目录（search 默认枚举的 Python 包装类；父类自动加入）
_GRAPH_SEARCH_LIBRARIES = ("KismetSystemLibrary", "GameplayStatics")

#: 流程控制节点目录（kind="flow" 的合法 target）。via=macro 走 add_macro_node
#: （fake_unreal 同名集合同口径），via=node_by_class 走 add_node_by_class
#: （Branch 不是宏，是 K2Node_IfThenElse）。pins = (name, direction, type, hint)。
_APPLY_FLOW_CATALOG = {
    "Branch": {"node_class": "K2Node_IfThenElse", "via": "node_by_class",
               "pins": [("execute", "EGPD_Input", "exec", ""),
                        ("Condition", "EGPD_Input", "bool", ""),
                        ("then", "EGPD_Output", "exec", ""),
                        ("else", "EGPD_Output", "exec", "")]},
    "Sequence": {"node_class": "K2Node_MacroInstance", "via": "macro",
                 "pins": [("execute", "EGPD_Input", "exec", ""),
                          ("then", "EGPD_Output", "exec", ""),
                          ("then_2", "EGPD_Output", "exec", "")]},
    "ForEachLoop": {"node_class": "K2Node_MacroInstance", "via": "macro",
                    "pins": [("execute", "EGPD_Input", "exec", ""),
                             ("Array", "EGPD_Input", "array", ""),
                             ("LoopBody", "EGPD_Output", "exec", ""),
                             ("Array Element", "EGPD_Output", "object", ""),
                             ("Completed", "EGPD_Output", "exec", "")]},
    "WhileLoop": {"node_class": "K2Node_MacroInstance", "via": "macro",
                  "pins": [("execute", "EGPD_Input", "exec", ""),
                           ("Condition", "EGPD_Input", "bool", ""),
                           ("LoopBody", "EGPD_Output", "exec", ""),
                           ("Completed", "EGPD_Output", "exec", "")]},
    "DoOnce": {"node_class": "K2Node_MacroInstance", "via": "macro",
               "pins": [("execute", "EGPD_Input", "exec", ""),
                        ("Completed", "EGPD_Output", "exec", "")]},
    "Gate": {"node_class": "K2Node_MacroInstance", "via": "macro",
             "pins": [("Enter", "EGPD_Input", "exec", ""),
                      ("Open", "EGPD_Input", "exec", ""),
                      ("Close", "EGPD_Input", "exec", ""),
                      ("Exit", "EGPD_Output", "exec", "")]},
    "FlipFlop": {"node_class": "K2Node_MacroInstance", "via": "macro",
                 "pins": [("A", "EGPD_Output", "exec", ""),
                          ("B", "EGPD_Output", "exec", "")]},
    "MultiGate": {"node_class": "K2Node_MacroInstance", "via": "macro",
                  "pins": [("enter", "EGPD_Input", "exec", ""),
                           ("Out 0", "EGPD_Output", "exec", ""),
                           ("Out 1", "EGPD_Output", "exec", "")]},
    "IsValid": {"node_class": "K2Node_MacroInstance", "via": "macro",
                "pins": [("execute", "EGPD_Input", "exec", ""),
                         ("Input Object", "EGPD_Input", "object", ""),
                         ("is valid", "EGPD_Output", "exec", ""),
                         ("is not valid", "EGPD_Output", "exec", "")]},
}

#: 标准事件目录（kind="event" 命中的父类事件函数名 —— Actor 蓝图实测口径
#: ReceiveBeginPlay 而非 BeginPlay，见 game_thread_build.py.tmpl）。友好映射
#: 单独列出（确定性归一，非猜测）。
_APPLY_EVENT_CATALOG = {
    "ReceiveBeginPlay": {"pins": [("then", "EGPD_Output", "exec", "")]},
    "ReceiveTick": {"pins": [("then", "EGPD_Output", "exec", ""),
                             ("DeltaSeconds", "EGPD_Output", "float", "")]},
    "ReceiveEndPlay": {"pins": [("then", "EGPD_Output", "exec", ""),
                                ("EndPlayReason", "EGPD_Output", "byte", "")]},
    "ReceiveActorBeginOverlap": {"pins": [("then", "EGPD_Output", "exec", ""),
                                          ("OtherActor", "EGPD_Output", "object", "")]},
    "ReceiveActorEndOverlap": {"pins": [("then", "EGPD_Output", "exec", "")]},
    "ReceiveHit": {"pins": [("then", "EGPD_Output", "exec", "")]},
    "ReceiveAnyDamage": {"pins": [("then", "EGPD_Output", "exec", "")]},
    "ReceiveDestroyed": {"pins": [("then", "EGPD_Output", "exec", "")]},
}
#: 事件名友好映射（tmpl 同款：BeginPlay → ReceiveBeginPlay）
_APPLY_EVENT_ALIASES = {
    "BeginPlay": "ReceiveBeginPlay",
    "Event BeginPlay": "ReceiveBeginPlay",
    "Tick": "ReceiveTick",
    "EndPlay": "ReceiveEndPlay",
}

#: 函数图的结构性节点（非 EventGraph 清图时**必须保留** —— 删掉函数图就没法
#: 落任何节点了；空图判定同样排除它们，否则函数图永远「非空」）
_APPLY_STRUCTURAL_NODE_CLASSES = ("K2Node_FunctionEntry", "K2Node_FunctionResult")

#: 引擎函数引脚签名的诚实说明（纯 Python 通道无法离线读 UFunction 反射签名 ——
#: UEBox 用 C++ GetAllFunctions 通道，本版零 C++；候选名照给，签名如实缺省）
_GRAPH_SEARCH_ENGINE_PINS_NOTE = (
    "引擎函数引脚签名需 C++ 枚举通道（本版纯 Python 未实现）："
    "先用本条目参与 apply_graph，应用后经 read_nodes 看实际引脚；"
    "连线/编译失败信息会给出正确引脚名")


def _snake_to_camel(name: str) -> str:
    """snake_case → CamelCase（引擎 Python 包装方法名 → UE 反射函数名：
    print_string → PrintString；k2_set_actor_location → K2_SetActorLocation）。"""
    parts = str(name or "").split("_")
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def _bp_being_compiled(bp) -> tuple:
    """读蓝图「编译中」标志 → (being_compiled, guard_active, note)。

    为什么多 getter 兜底（与 _bp_status_str 同风格）：bBeingCompiled 是
    UBlueprint 的 UPROPERTY，UE Python 读法随版本/DLL 而异 —— 全部读不到时
    **放行并告警**（守卫未生效 ≠ 一律拒绝：把「读不到」当「编译中」会让所有
    写操作在老 DLL 上全灭，违反 R1 假阴性纪律）。真实引擎里编辑器正在编译时
    该标志必然可读（编译循环自己置位）。
    """
    for getter in (lambda: bp.get_editor_property("b_being_compiled"),
                   lambda: getattr(bp, "b_being_compiled", None),
                   lambda: getattr(bp, "bBeingCompiled", None),
                   lambda: getattr(bp, "being_compiled", None)):
        try:
            val = getter()
        except Exception:
            continue
        if isinstance(val, bool):
            return val, True, None
    return False, False, (
        "bBeingCompiled 引擎包装器不暴露（5.1/5.8 真机 debug_reflect 实测："
        "dir() 无任何 compil 成员）→ 标志守卫不可用；实际防线 = 全部写操作与"
        "编译经 GT 队列串行化（同线程不交错），本守卫仅防外部异步编译，"
        "如遇并发写冲突请人工确认")


def _graph_apply_read_pins(graph, node) -> List[dict]:
    """读节点完整引脚签名（与 _read_l2_nodes 同源：get_pin_infos）。

    类型名走既有归一化口径：pin_category 优先（exec/bool/object/...），
    缺省回退 sub_category。is_delegate 按 category==delegate 判定。
    读取失败 → 空列表（只读搜索不因单节点引脚异常而中断）。
    """
    out: List[dict] = []
    try:
        pins = bridge.get_pin_infos(graph, node) or []
    except Exception:
        return out
    for p in pins:
        cat = str(getattr(p, "pin_category", "") or "")
        sub = str(getattr(p, "pin_sub_category", "") or "")
        out.append({
            "name": str(getattr(p, "pin_name", "?") or "?"),
            "type": cat or sub or "?",
            "direction": str(getattr(p, "direction", "") or ""),
            "is_delegate": cat.lower() == "delegate",
            "default_hint": str(getattr(p, "default_value", "") or ""),
        })
    return out


def _cmd_search_blueprint_nodes(bp_path: str, query: str = "",
                                limit: int = 20) -> dict:
    """「查一批」通道：按名字搜可调用函数/事件/流程控制/变量，连完整引脚签名。

    T-20261024-UE5BRIDGE-GRAPH-APPLY · 只读命令。规格书：没有这个只读命令，
    整图写入不成立（模型需要先拿到准确引脚签名才能写对 spec）。

    搜索源（按可信度排序，签名可信度递减）：
      ① 图内已有节点 —— 完整签名（get_pin_infos，与 _read_l2_nodes 同源）
      ② 蓝图自有函数图 —— 入口/出口引脚聚合
      ③ 蓝图变量 —— get/set 候选 + 类型合成签名
      ④ 流程控制目录 / ⑤ 标准事件目录 —— 内置确定性目录
      ⑥ 父类函数 / 函数库候选 —— dir() 枚举候选名，签名如实缺省（见
         _GRAPH_SEARCH_ENGINE_PINS_NOTE）
    query 子串匹配（大小写不敏感；空串 = 全量浏览）；limit 上限 50。
    """
    bp = _resolve_blueprint(bp_path)
    warnings: List[str] = []
    # limit 收敛（>50 → clamp + warning，不 fail —— 防洪水是保护机制不是校验）
    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        raise BridgeError(f"limit 必须为整数: {limit!r}", category="param_type")
    clamped = False
    if limit_i < 1:
        limit_i = 1
    if limit_i > _GRAPH_SEARCH_LIMIT_MAX:
        limit_i = _GRAPH_SEARCH_LIMIT_MAX
        clamped = True
        warnings.append(f"limit 超上限，已收敛为 {_GRAPH_SEARCH_LIMIT_MAX}")

    ql = str(query or "").strip().lower()
    results: List[dict] = []

    def _match(*names) -> bool:
        return not ql or any(ql in str(n or "").lower() for n in names)

    # ── ① 图内已有节点（签名最可信：引擎真实引脚面）───────────────────
    try:
        graphs = bridge.get_all_graphs(bp) or []
    except Exception:
        graphs = []
    if not graphs:
        try:
            g0 = bridge.get_event_graph(bp)
            if g0:
                graphs = [g0]
        except Exception:
            graphs = []
    own_function_graph_names: List[str] = []
    for graph in graphs:
        try:
            nodes = bridge.list_nodes(graph) or []
        except Exception:
            continue
        try:
            gname = str(graph.get_name())
        except Exception:
            gname = "?"
        has_entry = False
        for n in nodes:
            ncls = str(getattr(n, "node_class", "") or "")
            if ncls == "K2Node_FunctionEntry":
                has_entry = True
            title = _pywrap_node_title(n)
            if not title or not _match(title):
                continue
            results.append({
                "kind": _graph_apply_kind_of_class(ncls),
                "node_class": ncls or "?",
                "target": title,
                "source": "graph",
                "class": "",
                "pins": _graph_apply_read_pins(graph, n),
                "graph": gname,
            })
        if has_entry:
            own_function_graph_names.append(gname)

    # ── ② 蓝图自有函数图（入口/出口引脚聚合 → spec 可直接引用）─────────
    try:
        bp_name = str(bp.get_name())
    except Exception:
        bp_name = ""
    for gname in own_function_graph_names:
        if not _match(gname):
            continue
        pins: List[dict] = []
        for graph in graphs:
            try:
                if str(graph.get_name()) != gname:
                    continue
            except Exception:
                continue
            for n in (bridge.list_nodes(graph) or []):
                ncls = str(getattr(n, "node_class", "") or "")
                if ncls not in _APPLY_STRUCTURAL_NODE_CLASSES:
                    continue
                # 入口节点：输入引脚即函数参数；出口节点：输出引脚即返回值
                for p in _graph_apply_read_pins(graph, n):
                    if ncls == "K2Node_FunctionEntry" and \
                            p["direction"] == "EGPD_Input":
                        pins.append(p)
                    elif ncls == "K2Node_FunctionResult" and \
                            p["direction"] == "EGPD_Output":
                        pins.append(p)
        results.append({
            "kind": "function", "node_class": "K2Node_FunctionEntry",
            "target": gname, "source": "own_function",
            "class": bp_name, "pins": pins, "graph": gname,
        })

    # ── ③ 蓝图变量 → get/set 候选（类型合成签名）────────────────────
    try:
        raw_vars = bridge.read_variables(bp) or []
    except Exception:
        raw_vars = []
    for v in raw_vars:
        name = str(getattr(v, "variable_name", None)
                   or (v.get("variable_name") if isinstance(v, dict) else "")
                   or "")
        vtype = str(getattr(v, "variable_type", None)
                    or (v.get("variable_type") if isinstance(v, dict) else "")
                    or "")
        if not name or not _match(name):
            continue
        for kind, direction, node_class in (
                ("variable_get", "EGPD_Output", "K2Node_VariableGet"),
                ("variable_set", "EGPD_Input", "K2Node_VariableSet")):
            pins = [{"name": name, "type": vtype or "var",
                     "direction": direction, "is_delegate": False,
                     "default_hint": ""}]
            if kind == "variable_set":
                pins.append({"name": "then", "type": "exec",
                             "direction": "EGPD_Output", "is_delegate": False,
                             "default_hint": ""})
            results.append({
                "kind": kind, "node_class": node_class, "target": name,
                "source": "variable", "class": "", "pins": pins,
            })

    # ── ④ 流程控制目录 / ── ⑤ 标准事件目录（确定性内置）────────────────
    for target, meta in _APPLY_FLOW_CATALOG.items():
        if not _match(target):
            continue
        results.append({
            "kind": "flow", "node_class": meta["node_class"],
            "target": target, "source": "flow_catalog", "class": "",
            "pins": [{"name": p[0], "type": p[2], "direction": p[1],
                      "is_delegate": False, "default_hint": p[3]}
                     for p in meta["pins"]],
        })
    for target, meta in _APPLY_EVENT_CATALOG.items():
        if not _match(target):
            continue
        results.append({
            "kind": "event", "node_class": "K2Node_Event",
            "target": target, "source": "event_catalog", "class": "",
            "pins": [{"name": p[0], "type": p[2], "direction": p[1],
                      "is_delegate": False, "default_hint": p[3]}
                     for p in meta["pins"]],
        })

    # ── ⑥ 父类函数 / 函数库候选（dir() 枚举；签名如实缺省）────────────
    candidates: List[str] = list(_GRAPH_SEARCH_LIBRARIES)
    try:
        parent = str(bp.generated_class().get_super_class().get_name())
        if parent and parent not in candidates:
            candidates.insert(0, parent)
    except Exception:
        warnings.append("父类名不可读 → 父类函数候选通道跳过")
    for cls_name in candidates:
        py_type = getattr(unreal, cls_name, None)
        if py_type is None:
            # 函数库类在 Python 包装不存在（离线替身 / 老版本）→ 跳过，不告警
            # （父类缺失已在上面告警过；此处静默跳过避免噪声）
            continue
        try:
            attrs = dir(py_type)
        except Exception:
            continue
        for attr in attrs:
            if attr.startswith("_"):
                continue
            try:
                if not callable(getattr(py_type, attr, None)):
                    continue
            except Exception:
                continue
            camel = _snake_to_camel(attr)
            if not _match(attr, camel):
                continue
            results.append({
                "kind": "function", "node_class": "K2Node_CallFunction",
                "target": camel, "source": "engine_class",
                "class": cls_name, "pins": [],
                "pins_note": _GRAPH_SEARCH_ENGINE_PINS_NOTE,
            })

    total = len(results)
    truncated = total > limit_i
    return {
        "success": True,
        "query": str(query or ""),
        "total_matches": total,
        "returned": min(total, limit_i),
        "truncated": truncated,
        "results": results[:limit_i],
        "warnings": warnings,
    }


def _graph_apply_kind_of_class(node_class: str) -> str:
    """节点类 → GraphSpec kind（图内已有节点的分类展示用）。"""
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


def _graph_spec_parse(graph_spec: dict) -> dict:
    """GraphSpec JSON 解析 + 校验（容错缺省 + 非法即 fail-loud）。

    容错策略（规格书：模型生成 JSON，解析面必须宽容**形态**、严格**语义**）：
      · graph 缺省 → "EventGraph"（主路径图）；
      · kind 别名归一：get/set → variable_get/variable_set；
      · edges 缺省 → []（只落节点不连线合法）；pin_defaults 缺省 → {}；
      · 节点可选 pos（跳过自动布局的显式坐标）。
    严格语义（fail-loud，绝不带病执行）：
      · nodes 必须为非空数组；id 非空 / 无点号 / 唯一（edges 用 "id.pin" 点语法，
        id 含点会切错位）；
      · kind ∈ {event, function, flow, variable_get, variable_set}；
      · edge 的 from/to 必须形如 "<id>.<pin>" 且 id 已在 nodes 中声明；
      · pin_defaults 的键同样 "<id>.<pin>"。
    """
    if not isinstance(graph_spec, dict):
        raise BridgeError(
            "graph_spec 必须为对象（GraphSpec JSON，见 SKILL.md §3.7）",
            category="param_type",
            suggestion='{"graph": "EventGraph", "nodes": [...], "edges": [...]}')
    graph_name = str(graph_spec.get("graph") or "EventGraph").strip() or "EventGraph"

    raw_nodes = graph_spec.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise BridgeError(
            "graph_spec.nodes 必须为非空数组（整图写入至少落一个节点）",
            category="param_type")
    kind_aliases = {"get": "variable_get", "set": "variable_set",
                    "variable": "variable_get"}
    ids = set()
    parsed_nodes = []
    for i, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            raise BridgeError(
                f"nodes[{i}] 应为对象，实收 {type(raw).__name__}",
                category="param_type")
        nid = str(raw.get("id") or "").strip()
        if not nid or "." in nid:
            raise BridgeError(
                f"nodes[{i}].id 非法: {nid!r}（不可为空 / 不可含点号 —— "
                "edges 以 \"<id>.<pin>\" 点语法引用节点）",
                category="param_type")
        if nid in ids:
            raise BridgeError(f"nodes[{i}].id 重复: {nid}", category="param_type")
        ids.add(nid)
        kind = str(raw.get("kind") or "").strip().lower()
        kind = kind_aliases.get(kind, kind)
        if kind not in ("event", "function", "flow",
                        "variable_get", "variable_set"):
            raise BridgeError(
                f"nodes[{i}].kind 非法: {kind!r}（合法: event / function / "
                "flow / variable_get / variable_set）",
                category="param_type")
        target = str(raw.get("target") or "").strip()
        if not target:
            raise BridgeError(
                f"nodes[{i}].target 必填（函数名 / 事件名 / 宏名 / 变量名）",
                category="param_type",
                suggestion="先用 search_blueprint_nodes 拿准确的 target 与引脚签名")
        pos = raw.get("pos")
        pos_xy = None
        if pos is not None:
            try:
                pos_xy = (int(pos[0]), int(pos[1]))
            except Exception:
                raise BridgeError(
                    f"nodes[{i}].pos 应为 [x, y] 数值数组: {pos!r}",
                    category="param_type")
        parsed_nodes.append({
            "id": nid, "kind": kind, "target": target,
            "class": str(raw.get("class") or "").strip(),
            "pos": pos_xy,
        })

    raw_edges = graph_spec.get("edges")
    if raw_edges is None:
        raw_edges = []
    if not isinstance(raw_edges, list):
        raise BridgeError("graph_spec.edges 应为数组", category="param_type")
    parsed_edges = []
    for i, raw in enumerate(raw_edges):
        if not isinstance(raw, dict):
            raise BridgeError(f"edges[{i}] 应为对象", category="param_type")
        pts = {}
        for side in ("from", "to"):
            ref = str(raw.get(side) or "").strip()
            if "." not in ref:
                raise BridgeError(
                    f"edges[{i}].{side} 非法: {ref!r}（应为 \"<节点id>.<引脚名>\"）",
                    category="param_type")
            nid, pin = ref.split(".", 1)
            nid, pin = nid.strip(), pin.strip()
            if nid not in ids:
                raise BridgeError(
                    f"edges[{i}].{side} 引用了未声明的节点 id: {nid!r}",
                    category="param_type")
            if not pin:
                raise BridgeError(
                    f"edges[{i}].{side} 引脚名为空", category="param_type")
            pts[side] = (nid, pin)
        parsed_edges.append({"from": pts["from"], "to": pts["to"]})

    raw_defaults = graph_spec.get("pin_defaults")
    if raw_defaults is None:
        raw_defaults = {}
    if not isinstance(raw_defaults, dict):
        raise BridgeError("graph_spec.pin_defaults 应为对象", category="param_type")
    parsed_defaults = []
    for key, val in raw_defaults.items():
        ref = str(key or "").strip()
        if "." not in ref:
            raise BridgeError(
                f"pin_defaults 键非法: {key!r}（应为 \"<节点id>.<引脚名>\"）",
                category="param_type")
        nid, pin = ref.split(".", 1)
        nid, pin = nid.strip(), pin.strip()
        if nid not in ids:
            raise BridgeError(
                f"pin_defaults 引用了未声明的节点 id: {nid!r}",
                category="param_type")
        parsed_defaults.append({"id": nid, "pin": pin, "value": str(val)})

    return {"graph": graph_name, "nodes": parsed_nodes,
            "edges": parsed_edges, "pin_defaults": parsed_defaults}


def _graph_apply_layout(spec: dict) -> Dict[str, tuple]:
    """确定性自动布局：拓扑序分层网格（纯 Python，无外部依赖）。

    算法（规格书：拓扑序网格；禁 elkjs —— EPL-2.0 许可边界）：
      · 列 = 拓扑层：Kahn 分层（入度 0 → 层 0，逐层剥）；有环剩余节点
        按声明序追加到最后一层（确定性，不报错 —— 图合法性与编译器无关，
        布局只求「稳定可读」）；
      · 行 = 层内声明序；
      · 坐标 = (col × 300, row × 240)，原点 (0,0)；
      · 显式 pos 的节点跳过布局（模型自己给的坐标优先）。
    相同 spec → 相同坐标（测试计划第 8 组断言）。
    """
    nodes = spec["nodes"]
    edges = spec["edges"]
    id_order = [n["id"] for n in nodes]
    indeg = {nid: 0 for nid in id_order}
    succ = {nid: [] for nid in id_order}
    for e in edges:
        a, b = e["from"][0], e["to"][0]
        if a in indeg and b in indeg:
            indeg[b] += 1
            succ[a].append(b)
    layer = {nid: None for nid in id_order}
    frontier = [nid for nid in id_order if indeg[nid] == 0]
    cur = 0
    while frontier:
        nxt = []
        for nid in frontier:
            layer[nid] = cur
        for nid in frontier:
            for b in succ[nid]:
                indeg[b] -= 1
                if indeg[b] == 0 and layer[b] is None:
                    nxt.append(b)
        frontier = nxt
        cur += 1
    # 有环剩余（layer 仍为 None）→ 追加到最后一层（按声明序，确定性）
    for nid in id_order:
        if layer[nid] is None:
            layer[nid] = cur
    rows: Dict[int, int] = {}
    pos = {}
    for nid in id_order:
        nspec = next(n for n in nodes if n["id"] == nid)
        if nspec["pos"] is not None:
            pos[nid] = nspec["pos"]
            continue
        col = layer[nid]
        row = rows.get(col, 0)
        rows[col] = row + 1
        pos[nid] = (col * 300, row * 240)
    return pos


def _graph_apply_list_nodes(graph) -> List[dict]:
    """图的节点清单 → [{guid, title, node_class}]（守卫/快照/清图共用）。"""
    try:
        infos, err = _pywrap_readback_nodes(graph)
    except Exception as e:
        raise BridgeError(f"读取节点失败: {e}", category="graph_failure")
    if err:
        raise BridgeError(f"读取节点失败: {err}", category="graph_failure")
    return [{"guid": i.guid, "title": i.title, "node_class": i.node_class}
            for i in infos]


def _graph_apply_clear(graph, keep_classes: tuple) -> tuple:
    """replace 清空：删除非结构性节点 → (deleted_titles, errors)。

    删除走既有引用检查原语（bridge.remove_node，内部 Graph->Modify + 引用
    处理）；引擎拒绝删除（被外部引用等）→ 逐条收集并失败（fail-loud，
    绝不静默跳过 —— 规格书：被外部引用的节点列出并失败）。
    """
    deleted: List[str] = []
    errors: List[str] = []
    for info in _graph_apply_list_nodes(graph):
        if info["node_class"] in keep_classes:
            continue
        if not info["guid"]:
            errors.append(f"节点 {info['title']!r} 无 GUID，无法安全删除")
            continue
        node = bridge.get_node_by_guid(graph, info["guid"])
        if node is None:
            errors.append(f"节点 {info['title']!r} 无法从 GUID 还原对象")
            continue
        try:
            ok = bridge.remove_node(graph, node)
        except Exception as e:
            ok = False
            errors.append(f"删除 {info['title']!r} 异常: {e}")
        if ok:
            deleted.append(info["title"])
        else:
            errors.append(
                f"删除 {info['title']!r} 被拒绝（可能被外部引用，引擎拒绝）")
    return deleted, errors


def _graph_apply_rollback(graph, created: List[tuple], before_guids: set) -> dict:
    """事务回滚：逆序删除本次创建的节点 → 复读验证回到事务前状态。

    这是 Python 侧事务的实现方式（对齐 /build-batch 的「失败回滚已建资产」
    模式）：引擎事务 API（ScopedTransaction）不可用于跨调用回滚，改为
    「显式追踪 + 逆序清理 + 复读确认」，任何清理失败如实上报（fail-loud）。
    """
    removed: List[str] = []
    errors: List[str] = []
    for sid, node, _guid in reversed(created):
        try:
            ok = bridge.remove_node(graph, node)
        except Exception as e:
            errors.append(f"回滚删除 {sid} 异常: {e}")
            continue
        if ok:
            removed.append(sid)
        else:
            errors.append(f"回滚删除 {sid} 被拒绝")
    clean = False
    remaining: List[dict] = []
    try:
        now = {i["guid"] for i in _graph_apply_list_nodes(graph) if i["guid"]}
        # 回读通道可用时：剩余节点应恰好是事务前快照（结构性节点保留）
        clean = (not errors) and (now == set(before_guids))
        remaining = [i for i in _graph_apply_list_nodes(graph)]
    except Exception as e:
        errors.append(f"回滚后复读失败: {e}")
    return {"attempted": True, "removed": removed, "clean": clean,
            "errors": errors, "remaining": remaining}


def _graph_apply_create_node(bp, graph, nspec: dict, pos: tuple, warnings):
    """按 spec 建一个节点 → 节点对象。失败抛 BridgeError（事务回滚触发器）。

    复用既有原语的引擎调用层（规格书执行序第 3 步）：
      event   → 标准事件 add_event_node（目录命中）/ 自定义事件 add_custom_event_node
      function→ add_function_call_node（K2_ 候选重试与 _cmd_add_function_node 同款）
      flow    → add_macro_node（宏）/ add_node_by_class（Branch 等非宏）
      variable_get/set → add_variable_node（变量存在性前置校验，绝不猜）
    """
    kind = nspec["kind"]
    target = nspec["target"]
    x, y = int(pos[0]), int(pos[1])

    if kind == "event":
        ev_name = _APPLY_EVENT_ALIASES.get(target, target)
        if ev_name in _APPLY_EVENT_CATALOG:
            node = _safe_call(bridge.add_event_node, graph, ev_name, x, y,
                              error_cat="node_add")
            if node is None:
                raise BridgeError(
                    f"add_event_node returned None（事件 {ev_name} 建节点失败）"
                    f"—— 父类可能不含该事件；可用事件见 search_blueprint_nodes"
                    f"（source=event_catalog）",
                    category="node_add")
            # 对齐 /build 模板：标准事件需覆盖父类函数标记
            try:
                if hasattr(node, "bOverrideFunction"):
                    node.bOverrideFunction = True
            except Exception:
                pass
            return node
        # 不在标准目录 → 自定义事件（确定性归一：非标准名 = 自定义）
        node = _safe_call(bridge.add_custom_event_node, graph, target, x, y,
                          error_cat="node_add")
        if node is None:
            raise BridgeError(
                f"add_custom_event_node returned None（自定义事件 {target} "
                "建节点失败）", category="node_add")
        return node

    if kind == "function":
        fn_name, cls_name = target, nspec["class"]
        # "Class:Function" 形态（function_path 惯例）→ 拆分
        if ":" in target:
            cls_name, _, fn_name = target.rpartition(":")
            cls_name, fn_name = cls_name.strip(), fn_name.strip()
        if not cls_name:
            # 蓝图自有函数：同名函数图存在 → 类 = 本蓝图
            try:
                own_graphs = {str(g.get_name()) for g in
                              (bridge.get_all_graphs(bp) or [])}
            except Exception:
                own_graphs = set()
            if fn_name in own_graphs:
                try:
                    cls_name = str(bp.get_name())
                except Exception:
                    cls_name = ""
            if not cls_name:
                raise BridgeError(
                    f"function 节点 {nspec['id']!r} 无法定位类 —— spec 节点需带 "
                    f"\"class\"（search_blueprint_nodes 的 engine_class / "
                    f"own_function 命中会给出），或用 \"类:函数\" 形态的 target",
                    category="param_type")
        node = _safe_call(bridge.add_function_call_node, graph, fn_name,
                          cls_name, x, y, error_cat="node_add")
        # K2_ 候选重试（与 _cmd_add_function_node 同款：确定性名字归一，
        # 换名成功显式告警，不静默）
        if node is None:
            for cand in _function_name_candidates(fn_name)[1:]:
                node = _safe_call(bridge.add_function_call_node, graph, cand,
                                  cls_name, x, y, error_cat="node_add")
                if node is not None:
                    warnings.append(
                        f"函数名 {fn_name!r} 反射不可解析 → 已自动改用候选 "
                        f"{cand!r} 建节点（{nspec['id']}）")
                    break
        if node is None:
            raise BridgeError(
                f"add_function_call_node returned None（{cls_name}:{fn_name}）"
                "—— 常见原因：① 函数名不存在 ② 非 UFUNCTION（缺 BlueprintCallable）"
                " ③ 类名拼写错误；候选=" + str(_function_name_candidates(fn_name)),
                category="node_add")
        return node

    if kind == "flow":
        meta = _APPLY_FLOW_CATALOG.get(target)
        if meta is None:
            raise BridgeError(
                f"flow 节点 {nspec['id']!r}: 未知流程控制节点 {target!r} —— "
                "合法值见 search_blueprint_nodes（source=flow_catalog）",
                category="param_type")
        if meta["via"] == "macro":
            node = _safe_call(bridge.add_macro_node, graph, target, x, y,
                              error_cat="node_add")
        else:
            cls_obj = _pywrap_resolve_node_class(meta["node_class"])
            node = _safe_call(bridge.add_node_by_class, graph, cls_obj, x, y,
                              error_cat="node_add")
        if node is None:
            raise BridgeError(
                f"flow 节点 {target} 建节点返回 None（{meta['via']} 通道）",
                category="node_add")
        return node

    # variable_get / variable_set
    var_names = []
    try:
        for v in (bridge.read_variables(bp) or []):
            nm = str(getattr(v, "variable_name", None)
                     or (v.get("variable_name") if isinstance(v, dict) else "")
                     or "")
            if nm:
                var_names.append(nm)
    except Exception:
        pass
    if target not in var_names:
        raise BridgeError(
            f"variable 节点 {nspec['id']!r}: 蓝图中不存在变量 {target!r}"
            f"（现有: {var_names or '无'}）—— 绝不为不存在的变量建节点",
            category="param_type")
    node = _safe_call(bridge.add_variable_node, graph, target,
                      kind == "variable_set", x, y, error_cat="node_add")
    if node is None:
        raise BridgeError(
            f"add_variable_node returned None（{kind} {target}）",
            category="node_add")
    return node


def _graph_apply_connect(graph, node_by_id: Dict[str, object],
                         edge: dict, unique_titles: bool) -> tuple:
    """按 spec 连一条边 → (ok, reason)。复用 connect_pins 的拓扑验证语义。

    节点对象在事务内自持（无标题反查 → 同名节点天然无歧义）；
    验证分层：
      ① connect_pins 返回值（引擎建链）
      ② 引脚级回读：get_pin_infos 双端 is_connected（标题无关，同名安全）
      ③ 拓扑回读：get_node_connections 按标题比对 —— 仅当 spec 内标题唯一
        （同名场景比对必错位 → 降级为 ② 并告警，宁缺毋滥）
    """
    src_id, src_pin = edge["from"]
    dst_id, dst_pin = edge["to"]
    src_node = node_by_id[src_id]
    dst_node = node_by_id[dst_id]
    try:
        ok = bridge.connect_pins(src_node, src_pin, dst_node, dst_pin)
    except Exception as e:
        return False, f"连线异常 {src_id}.{src_pin} → {dst_id}.{dst_pin}: {e}"
    if not ok:
        return False, (f"ConnectPins 失败: {src_id}.{src_pin} → "
                       f"{dst_id}.{dst_pin}（引脚不存在或类型不可连）")
    # ② 引脚级回读（同 _connect_with_triple_check 的验证思想，但不需要标题定位）
    for side_node, side_id, side_pin in ((src_node, src_id, src_pin),
                                         (dst_node, dst_id, dst_pin)):
        connected = False
        try:
            for p in (bridge.get_pin_infos(graph, side_node) or []):
                if str(getattr(p, "pin_name", "")) == side_pin and \
                        bool(getattr(p, "is_connected", False)):
                    connected = True
                    break
        except Exception:
            connected = None  # 回读通道异常 → 不确定，交给 ③ 或最终读回验证
        if connected is False:
            return False, (f"连线回读未生效: {side_id}.{side_pin} "
                           "（get_pin_infos is_connected=False）")
    # ③ 拓扑回读（标题唯一才可靠）
    if unique_titles:
        src_title = _pywrap_node_title(src_node) or ""
        dst_title = _pywrap_node_title(dst_node) or ""
        # T-20261025-VERIFY-FIX：新建节点的 node_title 在 5.8 的 DLL 裸包装上
        # 不可达（恒 ''，与 H-1 的 NodeGuid 不可达同族）——标题取不到时跳过 ③
        # （② 引脚级回读已确认连线存在于两端引脚上），不再误判失败。
        # 旧行为：标题空 → 回退 spec id 比对 → 与连接读回的本地化标题
        # （如「事件BeginPlay」）必然不一致 → 每条新连线都被误判失败。
        if src_title and dst_title:
            try:
                conns = bridge.get_node_connections(graph) or []
            except Exception as e:
                return False, f"连线拓扑回读异常: {e}"
            found = any(
                str(getattr(c, "from_node_title", "")) == src_title and
                str(getattr(c, "from_pin_name", "")) == src_pin and
                str(getattr(c, "to_node_title", "")) == dst_title and
                str(getattr(c, "to_pin_name", "")) == dst_pin
                for c in conns)
            if not found:
                return False, (f"连线未在拓扑中确认: {src_title}.{src_pin} → "
                               f"{dst_title}.{dst_pin}")
    return True, ""


def _graph_apply_verify(graph, spec: dict, created: List[tuple],
                        before_guids: set) -> tuple:
    """读回验证 → (verified, warnings, errors)。

    比对项（规格书执行序第 5 步：重新读图，比对节点数/连线集合与 spec 一致）：
      · spec 声明的每个节点都在图中（GUID 锚点）—— 缺失 = 硬失败；
      · spec 声明的每条边都在图中（标题唯一时拓扑比对；否则引脚级比对）；
      · spec 之外多出的节点 → warning（隐式转换节点，R1 降级口径）不算失败。
    读回通道整体不可用 → verified=false + 告警（「验证没发生」是一等信号，
    但不回滚不阻断 —— R1：读不到 ≠ 没发生，制造假阴性比缺验证更糟）。
    """
    warnings: List[str] = []
    errors: List[str] = []
    try:
        infos, err = _pywrap_readback_nodes(graph)
    except Exception as e:
        return False, [f"读回验证通道异常（验证未发生）: {e}"], []
    if err:
        return False, [f"读回验证通道不可用（验证未发生）: {err}"], []

    by_guid = {i.guid: i for i in infos if i.guid}
    created_guids = {guid for _sid, _node, guid in created if guid}
    # GUID 锚点缺失的节点按标题弱排除（否则会把「自己的节点」误报成
    # spec 之外的隐式转换节点 —— 弱核对与 _cmd_add_function_node R6-4 同口径）
    created_titles = {_pywrap_node_title(node) for _sid, node, _g in created
                      if _pywrap_node_title(node)}
    # ① 节点在位性
    for sid, _node, guid in created:
        if guid and guid not in by_guid:
            errors.append(f"读回验证: 节点 {sid}（guid={guid[:8]}…）不在图中")
        elif not guid:
            warnings.append(
                f"读回验证: 节点 {sid} 的 GUID 锚点缺失，只能按标题弱核对")
    # ② 边在位性
    # T-20261025-VERIFY-FIX：创建节点的 wrapper 属性 node_title 在 5.8 上不可达
    # （恒 ''，B7 同族）—— 拓扑比对改用「读回节点的本地化标题」（经 GUID 映射）；
    # GUID/标题任一取不到 → 降级为引脚级验证（连接建立性已在逐边连线时验证）。
    title_by_guid = {i.guid: str(getattr(i, "node_title", "") or "")
                     for i in infos if i.guid}
    guid_of = {sid: guid for sid, _n, guid in created}
    titles_resolvable = bool(created) and all(
        title_by_guid.get(guid_of.get(sid, "")) for sid, _n, _g in created)
    unique_titles = len({(_pywrap_node_title(n) or sid)
                         for sid, n, _g in created}) == len(created)
    if unique_titles and titles_resolvable:
        try:
            conns = bridge.get_node_connections(graph) or []
        except Exception as e:
            conns = None
            errors.append(f"读回验证: 连线拓扑读取异常: {e}")
        if conns is not None:
            actual = {(str(getattr(c, "from_node_title", "")),
                       str(getattr(c, "from_pin_name", "")),
                       str(getattr(c, "to_node_title", "")),
                       str(getattr(c, "to_pin_name", ""))) for c in conns}
            for e in spec["edges"]:
                want = (title_by_guid.get(guid_of.get(e["from"][0], ""), e["from"][0]),
                        e["from"][1],
                        title_by_guid.get(guid_of.get(e["to"][0], ""), e["to"][0]),
                        e["to"][1])
                if want not in actual:
                    errors.append(
                        "读回验证: 连线缺失 "
                        f"{e['from'][0]}.{e['from'][1]} → "
                        f"{e['to'][0]}.{e['to'][1]}")
    else:
        reasons = []
        if not titles_resolvable:
            reasons.append("新建节点标题在 5.8 DLL 包装上不可达")
        if not unique_titles:
            reasons.append("spec 内存在同名节点标题")
        warnings.append(
            f"读回验证: {'、'.join(reasons) or '标题比对不可用'} → 连线拓扑比对"
            "（按标题）降级为引脚级验证（连接建立性已逐边验证），"
            "拓扑完整性需 read_connections 复核")
    # ③ spec 之外的额外节点 → warning（隐式转换节点，R1 口径）
    extra = [i for i in infos
             if i.guid and i.guid not in created_guids
             and i.guid not in (before_guids or set())
             and i.title not in created_titles]
    if extra:
        warnings.append(
            f"图中出现 {len(extra)} 个 spec 之外的节点"
            f"（隐式转换节点类，R1 口径不判失败）: "
            + ", ".join(i.title or i.guid[:8] for i in extra))
    verified = not errors
    return verified, warnings, errors


def _graph_apply_node_by_id(created: List[tuple], sid: str):
    """created 追踪表 → 节点对象（读回验证比对标题用；找不到返回 None）。"""
    for cid, node, _guid in created:
        if cid == sid:
            return node
    return None


def _cmd_apply_graph(bp_path: str, graph_spec=None, replace: bool = False) -> dict:
    """apply_graph 外壳：失败返回统一标 `_fail_loud`。

    T-20261024-UE5BRIDGE-GRAPH-APPLY：`_fail_loud` 是 /command 层的显式失败
    传播通道（v0.13 引入，delete_asset 首用）。apply_graph 是**写图主路径**
    的单次原子操作 —— 解析失败 / 守卫拒绝 / 事务回滚 / 读回不一致 / 编译失败
    任何一种都必须让调用方在端点层就看到失败，端点层假成功比内层失败更危险
    （模型会以为图已落好而直接进入下一步）。
    """
    result = _cmd_apply_graph_inner(bp_path, graph_spec, replace)
    if isinstance(result, dict) and not result.get("success"):
        result["_fail_loud"] = True
    return result


def _cmd_apply_graph_inner(bp_path: str, graph_spec=None,
                           replace: bool = False) -> dict:
    """整图声明式写入：一个事务内按 GraphSpec 建节点 + 连线 + 默认值。

    T-20261024-UE5BRIDGE-GRAPH-APPLY · 写图主路径（新蓝图创建仍走 build_blueprint）。

    执行序（规格书）：
      ① 守卫：bBeingCompiled → 409 语义拒绝；图非空且 replace≠true → 拒绝+摘要
      ② replace=true 清空目标图（结构性节点保留；引擎拒绝删除 → fail-loud）
      ③ 事务：建节点 → 连线（拓扑验证）→ pin_defaults → 提交
      ④ 自动布局（确定性拓扑序网格，失败仅告警）
      ⑤ 事务外编译（UEBox 纪律）
      ⑥ 读回验证 verified（多出隐式转换节点 → warning 不算失败）
      ⑦ 任何事务内失败 → 回滚整个事务，fail-loud

    返回：{success, compiled, compile_status, verified, nodes_applied,
    edges_applied, node_guids?, errors[], warnings[], rollback?, graph_summary?}
    —— success 键语义与全族一致（MCP isError 判据依赖）。
    """
    # spec 解析失败 → 结构化失败（category 透传给 /command 消费方；
    # 不裸抛 —— 参数错误也该带可读 category，而非端点层兜底文本）
    try:
        spec = _graph_spec_parse(graph_spec)
    except BridgeError as e:
        return {"success": False, "error": str(e), "category": e.category,
                "suggestion": e.suggestion, "compiled": False, "verified": False,
                "nodes_applied": 0, "edges_applied": 0,
                "errors": [str(e)], "warnings": []}
    _pywrap_require_bridge()
    bp = _resolve_blueprint(bp_path)
    graph = _pywrap_resolve_graph(bp, spec["graph"])
    warnings: List[str] = []
    errors: List[str] = []
    result = {
        "success": False, "compiled": False, "verified": False,
        "nodes_applied": 0, "edges_applied": 0,
        "graph": spec["graph"], "errors": errors, "warnings": warnings,
    }

    # ── ① 守卫一：编译中拒绝写入（409 Conflict 语义）──────────────────
    being, guard_active, guard_note = _bp_being_compiled(bp)
    if guard_note:
        warnings.append(guard_note)
    if being:
        errors.append(
            "蓝图正在编译中（bBeingCompiled=True）→ 拒绝整图写入"
            "（409 Conflict 语义）：编译期间图结构不稳定，写入会丢失或损坏")
        result["category"] = "graph_conflict"
        result["error"] = errors[0]
        return result

    # ── ① 守卫二：非空图必须显式 replace（让模型先 read 再决定）───────
    try:
        nodes_now = _graph_apply_list_nodes(graph)
    except BridgeError as e:
        result["category"] = e.category
        result["error"] = str(e)
        return result
    is_event_graph = spec["graph"] == "EventGraph"
    keep = () if is_event_graph else _APPLY_STRUCTURAL_NODE_CLASSES
    user_nodes = [n for n in nodes_now if n["node_class"] not in keep]
    if user_nodes and not replace:
        result["category"] = "graph_not_empty"
        result["error"] = (
            f"目标图 {spec['graph']} 非空（{len(user_nodes)} 个节点）—— "
            "整图写入是替换语义，需显式 replace=true；先 read_nodes 查看现图再决定")
        # 摘要（规格书：拒绝并返回当前图摘要，让模型先 read 再决定）
        result["graph_summary"] = {
            "graph": spec["graph"],
            "node_count": len(user_nodes),
            "node_titles": [n["title"] for n in user_nodes][:50],
            "structural_kept": [n["title"] for n in nodes_now
                                if n["node_class"] in keep],
        }
        return result

    # ── ② 引擎事务开始（T-20261027-TASK-UNDO）：写段 = 一个可回滚步 ────────
    #   除下方手工回滚外，再在 UE 原生事务栈里记一步（标题 [agent] apply_graph …）：
    #   写失败 → 手工复原后 Cancel（不留空撤销步）；成功 → 编译前提交（⑤），
    #   用户/agent 之后可整步回滚这张图。
    #   ⚠️ 事务开启后到 try 之间的代码不得抛异常（否则事务悬挂）：
    #   纯 Python 计算全部前置，唯一的外部读取用 try/except 兜住。
    layout = _graph_apply_layout(spec)
    tx_on = False
    if _AGENT_TX_ENABLED and _agent_tx_available():
        _short = str(bp_path).rstrip("/").split("/")[-1] or str(bp_path)
        tx_on = _agent_tx_begin(f"apply_graph {_short}")

    # ── ② replace 清空 ────────────────────────────────────────────────
    if replace and user_nodes:
        try:
            deleted, del_errors = _graph_apply_clear(graph, keep)
        except Exception as e:
            if tx_on:
                _agent_tx_cancel()
            result["category"] = "unknown"
            result["error"] = f"replace 清空异常：{type(e).__name__}: {e}"
            return result
        if del_errors:
            if tx_on:
                _agent_tx_cancel()
            errors.extend(
                f"replace 清空失败（图未改动，拒绝写入）: {e}" for e in del_errors)
            result["category"] = "node_delete"
            result["error"] = errors[0]
            result["replace_deleted"] = deleted
            return result
        warnings.append(
            f"replace=true 已清空 {len(deleted)} 个旧节点: "
            + ", ".join(deleted[:20]) + ("…" if len(deleted) > 20 else ""))
        try:
            nodes_now = _graph_apply_list_nodes(graph)
        except Exception as e:
            if tx_on:
                _agent_tx_cancel()
            result["category"] = "unknown"
            result["error"] = f"清空后回读节点失败：{type(e).__name__}: {e}"
            return result

    # ── ③ 事务开始：快照 + 建节点 + 连线 + 默认值 ─────────────────────
    before_guids = {n["guid"] for n in nodes_now if n["guid"]}
    created: List[tuple] = []          # [(spec_id, node_obj, guid)]
    try:
        node_by_id: Dict[str, object] = {}
        titles = {}
        for nspec in spec["nodes"]:
            pos = nspec["pos"] or layout.get(nspec["id"], (0, 0))
            node = _graph_apply_create_node(bp, graph, nspec, pos, warnings)
            guid = _pywrap_node_guid(node)
            if not guid:
                # 裸 K2Node 的 NodeGuid 在 UE5.1 常不可达 → 走回读兜底
                # （与 _cmd_add_function_node 的 R6-4 同口径）
                guid, w0 = _pywrap_recover_node_guid(graph, node, expect_pos=pos)
                warnings.extend(w0)
            created.append((nspec["id"], node, guid))
            node_by_id[nspec["id"]] = node
            titles[nspec["id"]] = _pywrap_node_title(node) or nspec["id"]
        unique_titles = len(set(titles.values())) == len(titles)
        for edge in spec["edges"]:
            ok, reason = _graph_apply_connect(graph, node_by_id, edge,
                                              unique_titles)
            if not ok:
                raise BridgeError(
                    f"{reason}（spec edge: {edge['from']} → {edge['to']}）",
                    category="pin_failure")
        for d in spec["pin_defaults"]:
            node = node_by_id.get(d["id"])
            ok = bridge.set_pin_default_value(node, d["pin"], d["value"])
            if not ok:
                raise BridgeError(
                    f"设置引脚默认值失败: {d['id']}.{d['pin']} = {d['value']!r}"
                    "（引脚不存在或类型不符）",
                    category="pin_failure")
            # 设值回读（fail-loud：写没写进去要确认，与 set_pin_default_value
            # 命令的回读纪律一致；回读通道异常不阻断 —— 编译期会暴露）
            try:
                got = None
                for p in (bridge.get_pin_infos(graph, node) or []):
                    if str(getattr(p, "pin_name", "")) == d["pin"]:
                        got = getattr(p, "default_value", None)
                        break
                if got is not None and str(got) != d["value"]:
                    warnings.append(
                        f"pin_defaults 回读不一致: {d['id']}.{d['pin']} 期望 "
                        f"{d['value']!r} 实读 {str(got)!r}（以引擎为准）")
            except Exception:
                pass
    except BridgeError as e:
        # ── ⑦ 事务内失败 → 回滚，fail-loud ──────────────────────────
        rb = _graph_apply_rollback(graph, created, before_guids)
        if tx_on:
            _agent_tx_cancel()   # 手工已复原 → 丢弃事务记录（不留空撤销步）
            tx_on = False
        result["category"] = e.category
        result["error"] = str(e)
        result["rollback"] = rb
        errors.append(f"事务已回滚（clean={rb['clean']}）: {e}")
        if rb["errors"]:
            errors.extend(f"回滚残留: {re}" for re in rb["errors"])
        if not rb["clean"]:
            errors.append("⚠️ 图已污染：回滚未完全复原，请 read_nodes 人工核对")
        return result
    except Exception as e:
        rb = _graph_apply_rollback(graph, created, before_guids)
        if tx_on:
            _agent_tx_cancel()
            tx_on = False
        result["category"] = "unknown"
        result["error"] = f"{type(e).__name__}: {e}"
        result["rollback"] = rb
        errors.append(f"事务已回滚（clean={rb['clean']}）: {type(e).__name__}: {e}")
        return result
    # ── 事务提交点（此后不再回滚：布局/编译/读回验证都不动图结构）──────
    if tx_on:
        _agent_tx_end()          # 提交：撤销栈里留下一个 [agent] apply_graph 步
        tx_on = False

    # ── ④ 自动布局（建节点时已带坐标；此处仅兜底校正 —— 失败仅告警）──
    try:
        for sid, node, _guid in created:
            want = next(n["pos"] for n in spec["nodes"] if n["id"] == sid) \
                or layout.get(sid)
            if want and (_pywrap_node_pos(node) != (int(want[0]), int(want[1]))):
                bridge.set_node_position(node, int(want[0]), int(want[1]))
    except Exception as e:
        warnings.append(f"自动布局失败（不影响图内容）: {e}")

    # ── ⑤ 事务外编译（UEBox 纪律；公共主体与 compile_blueprint 同源）──
    core = _compile_bp_and_scan_log(bp)
    compiled = core["compile_ok"]
    compile_status = _bp_status_str(bp, compiled=compiled)

    # ── ⑥ 读回验证 ────────────────────────────────────────────────────
    verified, v_warnings, v_errors = _graph_apply_verify(
        graph, spec, created, before_guids)
    warnings.extend(v_warnings)
    if v_errors:
        # 读回不一致 = fail-loud：报错不假成功（图已提交，不再回滚 ——
        # 引擎态不明时二次删除风险更高；如实报告 + 留 GUID 锚点供人工修复）
        errors.extend(v_errors)
        result["verified"] = False
        result["compile_status"] = compile_status
        result["error"] = "读回验证失败（图与 spec 不一致，绝不假成功）"
        result["nodes_applied"] = len(created)
        result["edges_applied"] = len(spec["edges"])
        result["node_guids"] = {sid: guid for sid, _n, guid in created}
        return result
    result["verified"] = True

    result["nodes_applied"] = len(created)
    result["edges_applied"] = len(spec["edges"])
    result["node_guids"] = {sid: guid for sid, _n, guid in created}

    # 编译失败 → success=false（MCP isError 置位）+ 结构化编译错误，
    # 图保留（§3.6 修复循环以此为起点；node_guids 即定位键）
    if not compiled:
        if core.get("structured"):
            cerrors, cwarnings = _structured_bp_errors(core["structured"], bp)
        else:
            cerrors, cwarnings = _extract_blueprint_errors(core["tail_lines"], bp)
        errors.extend(f"编译失败: {e.get('message', '?')}" for e in cerrors)
        warnings.extend(cwarnings)
        result["errors"] = cerrors or errors
        result["compile_status"] = compile_status
        result["error"] = (
            "整图已写入（verified=true）但编译失败 —— 走 §3.6 编译修复 SOP"
            "（ue5_compile_and_diagnose / 增量编辑工具）")
        return result

    result["success"] = True
    result["compiled"] = True
    result["compile_status"] = compile_status
    if core["log_matches"]:
        result["log_matches"] = core["log_matches"]
    return result


def _pywrap_node_pos(node) -> tuple:
    """读节点坐标（布局兜底校正用；读不到 → (None, None) 触发校正）。"""
    try:
        return int(getattr(node, "pos_x", 0)), int(getattr(node, "pos_y", 0))
    except Exception:
        return None, None


# ── 白名单命令表 ──
def _cmd_batch_asset_ops(items, dry_run: bool = True, confirm: bool = False,
                          stop_on_error: bool = True) -> dict:
    """批量重命名/搬迁资产（默认 dry-run 只出计划；真跑需 confirm=true）。

    items: [{"from": "/Game/A/BP_X", "to": "/Game/B/BP_X"}]（1..50）
    语义：目标已存在 → 冲突（不覆盖）；源不存在 → 失败；逐条回读验证。
    """
    if not isinstance(items, list) or not items:
        raise BridgeError("items 必须为非空数组（[{from,to}]）", category="param_type")
    if len(items) > 50:
        raise BridgeError("items 上限 50（一次搬太多请分批）", category="param_type")
    norm = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise BridgeError("items[%d] 不是对象" % i, category="param_type")
        src = _safe_game_pkg(it.get("from"))
        dst = _safe_game_pkg(it.get("to"))
        if not src or not dst:
            raise BridgeError("items[%d] from/to 非法（必须是规范 /Game/ 路径，"
                              "禁止 .. / 反斜杠）" % i, category="param_type")
        if src.rstrip("/") == dst.rstrip("/"):
            raise BridgeError("items[%d] from 与 to 相同" % i, category="param_type")
        norm.append({"from": src, "to": dst})
    if not dry_run and not confirm:
        raise BridgeError("batch_asset_ops 真跑会重命名/搬迁资产，需 confirm=true",
                          category="destructive_guard",
                          suggestion="先用 dry_run=true（默认）看计划")

    def _do():
        u = globals().get("unreal")
        eal = getattr(u, "EditorAssetLibrary", None) if u else None
        if eal is None:
            raise BridgeError("unreal.EditorAssetLibrary 不可用", category="asset_op")
        plan, errors = [], []
        for it in norm:
            src, dst = it["from"], it["to"]
            src_exists = bool(eal.does_asset_exist(src))
            dst_exists = bool(eal.does_asset_exist(dst))
            state = "ok"
            if not src_exists:
                state = "missing_source"
            elif dst_exists:
                state = "conflict_target_exists"
            plan.append({"from": src, "to": dst, "source_exists": src_exists,
                         "target_exists": dst_exists, "state": state})
            if state != "ok":
                errors.append("%s → %s: %s" % (src, dst, state))
        out = {"success": not errors, "dry_run": bool(dry_run),
               "planned": len(norm), "plan": plan, "errors": errors,
               "renamed": [], "failed": []}
        if dry_run or errors:
            if errors:
                out["_fail_loud"] = True
                out["error"] = "预检发现 %d 个问题（未执行任何变更）" % len(errors)
            return out

        for it in norm:
            src, dst = it["from"], it["to"]
            try:
                ok = bool(eal.rename_asset(src, dst))
                verified = (ok and bool(eal.does_asset_exist(dst))
                            and not bool(eal.does_asset_exist(src)))
                if verified:
                    out["renamed"].append({"from": src, "to": dst})
                else:
                    out["failed"].append({"from": src, "to": dst,
                                          "reason": "rename_asset 返回 %r / 回读不一致" % ok})
            except Exception as e:
                out["failed"].append({"from": src, "to": dst,
                                      "reason": "%s: %s" % (type(e).__name__, e)})
            if out["failed"] and stop_on_error:
                break
        out["success"] = not out["failed"]
        if out["failed"]:
            out["_fail_loud"] = True
            out["error"] = "批量操作有 %d 条失败（成功的已生效）" % len(out["failed"])
        return out

    return _gt_run_handler(_do, timeout=180.0)


def _cmd_task_assets(task_id: str = "") -> dict:
    """查看任务的资产账本（新建/删除/快照），回滚前先看它。"""
    tj = _task_journal_or_raise()
    try:
        ledger = tj.task_asset_ledger(task_id)
    except ValueError as e:
        raise BridgeError(str(e), category="task")
    return {"success": True, "ok": True, "data": ledger}


def _cmd_rollback_task_assets(task_id: str = "", confirm: bool = False) -> dict:
    """任务级资产回滚：删除本任务新建的资产 + 从快照恢复被删资产（v5.0）。

    · 只动任务账本里记录过的资产，绝不扫描/猜测；
    · 恢复是文件级：把快照写回原路径；编辑器内已加载的包需要重载
      （返回 reload_required=true 并给重载/重启提示）；
    · 修改类资产不在本命令范围（用 agent_undo 的事务回滚）。
    """
    if not confirm:
        raise BridgeError("rollback_task_assets 会删除/覆盖资产，需 confirm=true",
                          category="destructive_guard")
    tj = _task_journal_or_raise()
    try:
        ledger = tj.task_asset_ledger(task_id)
    except ValueError as e:
        raise BridgeError(str(e), category="task")
    tid = ledger.get("task_id")
    created = list(ledger.get("created_assets") or [])
    deleted = list(ledger.get("deleted_assets") or [])
    snaps = {s.get("asset"): s for s in (ledger.get("asset_snapshots") or [])
             if isinstance(s, dict)}

    out = {"success": True, "ok": True, "task_id": tid,
           "created_removed": [], "deleted_restored": [],
           "failed": [], "reload_required": False,
           "note": "修改类资产不在本命令范围（用 agent_undo）"}
    # ① 删除本任务新建的资产
    for path in created:
        try:
            exists = _gt_run_handler(
                lambda p=path: bool(unreal.EditorAssetLibrary.does_asset_exist(p)),
                timeout=30.0)
            if not exists:
                out["created_removed"].append({"asset": path, "status": "already_absent"})
                continue
            r = _delete_asset_cascade(path, 1)
            if r.get("success"):
                out["created_removed"].append({"asset": path, "status": "deleted"})
            else:
                out["failed"].append({"asset": path, "action": "delete",
                                      "error": r.get("error")})
        except Exception as e:
            out["failed"].append({"asset": path, "action": "delete",
                                  "error": "%s: %s" % (type(e).__name__, e)})

    # ② 从快照恢复被删资产（文件级）
    for path in deleted:
        snap = snaps.get(path)
        if not snap or not snap.get("snapshot"):
            out["failed"].append({"asset": path, "action": "restore",
                                  "error": "无删除前快照（任务未快照或快照丢失）"})
            continue
        try:
            def _restore(p=path, snap_path=snap["snapshot"]):
                content = unreal.Paths.convert_relative_path_to_full(
                    unreal.Paths.project_content_dir())
                _, rel = _asset_pkg_and_rel(p)
                if not rel:
                    raise BridgeError("非 /Game/ 路径: %s" % p, category="asset_op")
                dst = os.path.join(str(content), rel.replace("/", os.sep))
                if not os.path.isfile(snap_path):
                    raise BridgeError("快照文件不存在: %s" % snap_path,
                                      category="asset_op")
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(snap_path, dst)
                reloaded = None
                eal = getattr(unreal, "EditorAssetLibrary", None)
                fn = getattr(eal, "reload_asset", None) if eal else None
                if callable(fn):
                    try:
                        reloaded = bool(fn(p))
                    except Exception:
                        reloaded = False
                return {"restored": True, "reloaded": reloaded,
                        "bytes": os.path.getsize(dst)}
            info = _gt_run_handler(_restore, timeout=60.0) or {}
            out["deleted_restored"].append({"asset": path, **info})
            if not info.get("reloaded"):
                out["reload_required"] = True
        except Exception as e:
            out["failed"].append({"asset": path, "action": "restore",
                                  "error": "%s: %s" % (type(e).__name__, e)})

    if out["failed"]:
        out["success"] = False
        out["ok"] = False
        out["_fail_loud"] = True
        out["error"] = "资产回滚有 %d 项失败" % len(out["failed"])
    if out["reload_required"]:
        out["note"] += "；恢复的资产需要重载：重启编辑器或右键 Rescan"
    return out


def _cmd_debug_reflect(asset_path: str = "", probe: str = "") -> dict:
    """只读反射探针：加载资产 → 按候选名×两种读法探测属性 → 逐项返回值/异常。

    排查「UPROPERTY 随引擎/包装器的读法差异」（如 bBeingCompiled 是
    UPROPERTY(transient) 位域，get_editor_property/直取在真机上可能全失败）。
    probe 给定时对 dir() 做子串扫描（如 probe=compil 列出全部含该串成员）。
    GT 队列执行（unreal.* 纪律）。
    """
    asset_path = str(asset_path or "").strip()
    if not asset_path:
        raise BridgeError("asset_path 必填（如 /Game/Blueprints/BP_X）",
                          category="param_type")

    def _do():
        obj = unreal.EditorAssetLibrary.load_asset(asset_path)
        if not obj:
            return {"error": f"资产加载失败: {asset_path}"}
        out = {"asset": asset_path, "class": obj.get_class().get_name()}
        for cand in ("b_being_compiled", "bBeingCompiled", "being_compiled",
                     "b_is_regenerating_on_load", "status"):
            for how, getter in (
                ("get_editor_property",
                 lambda n=cand: obj.get_editor_property(n)),
                ("attr", lambda n=cand: getattr(obj, n)),
            ):
                try:
                    v = getter()
                    out[f"{how}:{cand}"] = repr(v)[:60]
                except Exception as e:
                    out[f"{how}:{cand}"] = f"FAIL {type(e).__name__}: {str(e)[:80]}"
        if probe:
            out["dir_scan"] = [a for a in dir(obj) if probe.lower() in a.lower()][:20]
        return out

    data = _gt_run_handler(_do)
    return {"success": True, "ok": True, "data": data}


# ── 任务日志命令（T-20261024-UI-V2 · 纯主机侧，零 UE/GT 依赖）──────────

def _task_journal_or_raise():
    if not _TASK_JOURNAL_AVAILABLE:
        raise BridgeError("任务日志模块不可用（task_journal 导入失败）",
                          category="dashboard")
    return _task_journal


_PANEL_AUTO_OPENED = False


def _maybe_open_panel() -> None:
    """任务开始时自动打开管理端（T-20261025-UX：用户要求"前端自动打开面板"）。

    进程内只开一次（防每个任务弹窗）；UE5_AUTO_OPEN_PANEL=0 可关闭。
    fail-open：打不开（无浏览器/无桌面）不影响任务。
    """
    global _PANEL_AUTO_OPENED
    try:
        if _PANEL_AUTO_OPENED:
            return
        if str(os.environ.get("UE5_AUTO_OPEN_PANEL", "1")).strip().lower() in (
                "0", "false", "off", "no"):
            return
        _PANEL_AUTO_OPENED = True
        port = int(os.environ.get("UE5_BRIDGE_PORT", "8889"))
        url = "http://127.0.0.1:%d/ui?token=%s" % (port, _BRIDGE_TOKEN or "")
        if hasattr(os, "startfile"):
            os.startfile(url)  # Windows：默认浏览器打开
    except Exception:
        pass


def _cmd_task_begin(params: dict = None) -> dict:
    """开始任务：goal 必填；user_request/understanding/approach/steps 建议带。"""
    tj = _task_journal_or_raise()
    try:
        out = {"success": True, "ok": True, "data": tj.task_begin(dict(params or {}))}
        _maybe_open_panel()
        return out
    except ValueError as e:
        raise BridgeError(f"task_begin 参数不合格: {e}", category="param_type",
                          suggestion="goal 一句话意图；steps=[{name,hint}] 计划步骤；"
                                     "understanding=蓝图意图分析；approach=实现思路")


def _cmd_task_step(params: dict = None) -> dict:
    """上报进度：step_id（按计划定位）或 note（自由注记）；evidence 带证据。"""
    tj = _task_journal_or_raise()
    try:
        return {"success": True, "ok": True, "data": tj.task_step(dict(params or {}))}
    except ValueError as e:
        raise BridgeError(f"task_step 参数不合格: {e}", category="param_type",
                          suggestion="step_id=计划步骤号 + status=running|done|failed|"
                                     "skipped；或 note=自由注记；evidence={screenshots:[],errors:0}")


def _save_dirty_assets(paths) -> dict:
    """GT：对资产 only_if_is_dirty 保存（T-20261027-SAVE 保存闭环）。

    只处理 /Game/ 内的资产路径；不存在的跳过（不报错——交付物可能含变量子路径）。
    """
    out = {"saved": [], "clean": [], "failed": [], "missing": []}
    try:
        eal = unreal.EditorAssetLibrary
    except Exception as e:
        return {"error": "EditorAssetLibrary 不可用: %s" % e}
    for p in (paths or [])[:40]:
        p = str(p or "").strip()
        if not p.startswith("/Game/"):
            continue
        try:
            if not eal.does_asset_exist(p):
                out["missing"].append(p)
                continue
            changed = bool(eal.save_asset(p, only_if_is_dirty=True))
            (out["saved"] if changed else out["clean"]).append(p)
        except Exception:
            out["failed"].append(p)
    return out


def _cmd_task_end(params: dict = None) -> dict:
    """收尾复盘：status=success|failed|aborted 必填；summary/lessons 建议带。

    收尾前做**保存闭环检查**（T-20261027-SAVE）：本任务触碰过的资产若还是脏的，
    自动 only_if_is_dirty 保存 —— 防「改完没存，关编辑器就没了」。
    UE5_AUTO_SAVE_ON_TASK_END=0 可关闭（只检查不保存）。
    """
    tj = _task_journal_or_raise()
    _params = dict(params or {})
    autosave = None
    try:
        paths = tj.task_touched_assets()
        if paths:
            if str(os.environ.get("UE5_AUTO_SAVE_ON_TASK_END", "1")).strip().lower()                     in ("0", "false", "off", "no"):
                autosave = {"skipped_all": True, "assets": paths, "saved": [],
                            "clean": [], "failed": [], "missing": []}
            else:
                autosave = _gt_run_handler(_save_dirty_assets, paths, timeout=120)
                autosave["assets"] = paths
            _params["_auto_save"] = autosave
    except Exception as e:
        _params["_auto_save"] = {"error": str(e)[:160]}
    _tid = tj.active_task_id()
    try:
        return {"success": True, "ok": True, "data": tj.task_end(_params)}
    except ValueError as e:
        raise BridgeError(f"task_end 参数不合格: {e}", category="param_type",
                          suggestion="summary=实现了什么；lessons=实操经验（下次避免"
                                     "简单问题复杂化/可采取的新方法）")
    finally:
        # 无论如何结束，都把该任务的资产锁释放（不靠 TTL 兜底）
        _release_locks(_tid)


def _cmd_task_list() -> dict:
    tj = _task_journal_or_raise()
    return {"success": True, "ok": True, "data": tj.task_list()}


def _cmd_task_error(params: dict = None) -> dict:
    """把一条错误注记挂到当前任务（撞错时顺手记经验素材）。"""
    tj = _task_journal_or_raise()
    try:
        return {"success": True, "ok": True, "data": tj.task_error_nudge(dict(params or {}))}
    except ValueError as e:
        raise BridgeError(f"task_error 参数不合格: {e}", category="param_type",
                          suggestion="note=错误一句话（做了什么→错在哪→怎么解的）")


# ═══════════════════════════════════════════════════════════════════════════
# 任务级撤销（T-20261027-UE5BRIDGE-TASK-UNDO · v4.5）
# ═══════════════════════════════════════════════════════════════════════════
# 写命令在 UE 原生事务栈里各留一步，标题统一 "[agent] <cmd> <资产名>"；回滚只
# 撤这些步，C++ 侧逐步校验标题，遇到用户/引擎的事务立刻停 —— Python 不做信任判断。
#
# 不包事务的命令（有意为之）：
#   · compile_* / build_blueprint / apply_graph —— 编译必须在事务外；apply_graph
#     自己在写入段开事务、编译前关闭（见 _cmd_apply_graph_inner）
#   · save_asset / delete_asset / unpin_console_refs —— 落盘与资产级操作不在
#     UE 撤销栈语义内（delete_asset 走高危确认）
#   · task_* / 只读命令 —— 不产生 UE 状态
#
# UE5_AGENT_TX=0 可整体关闭（排查用）；旧 DLL 无事务入口时自动降级为原行为。
_AGENT_TX_ENABLED = os.environ.get("UE5_AGENT_TX", "1") != "0"

_TRANSACTIONAL_COMMANDS = frozenset({
    "add_variable", "add_component", "add_function_node", "connect_pins",
    "set_pin_default_value", "create_node_by_class", "set_node_position",
    "add_variable_node", "add_custom_event_node", "add_input_key_event_node",
    "add_function_pin", "add_local_variable", "add_timeline_node",
    "add_comment_node", "set_class_default", "add_pin_to_node",
    "remove_pin_from_node", "try_node_add_input_pin", "set_node_property",
    "add_cast_node", "add_class_cast_node", "add_macro_node",
    "add_switch_node", "add_struct_node", "add_enum_literal_node",
    "add_composite_node", "add_math_expression_node", "add_async_action_node",
    "add_create_delegate_node", "add_actor_bound_event_node",
    "add_input_axis_event_node", "add_input_action_event_node",
    "add_component_bound_event_node", "remove_component",
    "add_implemented_interface", "add_event_dispatcher",
    # 断线/删节点（MCP 工具直连 /disconnect、/delete_node；白名单无同名命令，
    # 别名仅用于 MCP 包装）
    "disconnect_pins", "delete_node",
    # 材质 / UMG / 关卡 Actor：引擎侧若未记录则是空事务（End 时被引擎丢弃），无害
    "material_add_expression", "material_connect_expressions",
    "material_connect_property", "material_set_expression_property",
    "material_instance_set_parameter", "widget_add_child", "widget_set_property",
    "spawn_actor", "set_actor_transform",
})


def _agent_tx_available() -> bool:
    """桥 DLL 是否带事务入口（旧 DLL → 降级：写命令照常执行，只是不可撤销）。"""
    return (not _TEST_MODE) and BRIDGE_AVAILABLE and bridge is not None \
        and hasattr(bridge, "begin_agent_transaction")


def _agent_tx_begin(description: str) -> bool:
    try:
        return bool(bridge.begin_agent_transaction(str(description)))
    except Exception as e:
        _OP_LOG.warning(f"agent transaction begin failed: {e}")
        return False


def _agent_tx_end() -> None:
    try:
        bridge.end_agent_transaction()
    except Exception as e:
        _OP_LOG.warning(f"agent transaction end failed: {e}")


def _agent_tx_cancel() -> None:
    try:
        bridge.cancel_agent_transaction()
    except Exception as e:
        _OP_LOG.warning(f"agent transaction cancel failed: {e}")


def _agent_tx_title(cmd: str, params: dict) -> str:
    """撤销菜单标题：命令 + 资产短名（长路径只留末段，标题短才好读）。"""
    target = ""
    for key in ("bp_path", "asset_path", "widget_path", "material_path",
                "instance_path", "graph_name", "bp_name"):
        val = (params or {}).get(key)
        if val:
            target = str(val).strip()
            break
    if not target:
        return cmd
    short = target.rstrip("/").split("/")[-1] or target
    return f"{cmd} {short}"


# ══════════════════════════════════════════════════════════════════
# v6.3 · 资产锁 + 变更回执（对标 EngineWire consent/receipts 的轻量版）
# ══════════════════════════════════════════════════════════════════
_ASSET_LOCKS = {}
_ASSET_LOCK_TTL = max(30.0, float(os.environ.get("UE5_ASSET_LOCK_TTL", "300")))
_CONTENT_DIR_CACHE = {"path": ""}
_LOCKS_LOCK = threading.RLock()

# ── v6.5 · 显式会话锁（多 Agent 协作；任务锁之外的 opt-in 通道）──
# 任务锁（_ASSET_LOCKS）由 task 自动持有；会话锁给不建 task 的多个客户端用。
# 两者互相可见：任务写会检查会话锁，会话抢锁也会检查任务锁。
_SESSION_LOCKS = {}          # normalized asset key -> {owner, path, acquired_at, expires_at}
_SESSION_LOCK_TTL_DEFAULT = 600.0
_SESSION_LOCK_TTL_MAX = 86400.0
_SESSION_LOCK_MAX = 1024
_SESSION_LOCK_MAX_PER_OWNER = 256
_SESSION_LOCK_ASSETS_MAX = 200
_SESSION_LOCK_KEY_MAX = 512

# ── v6.5 · 幂等键（网络重试/agent 重试不重复写）──
_IDEMPOTENCY = {}            # key -> {ts, command, fingerprint, result}
_IDEMPOTENCY_LOCK = threading.Lock()
_IDEMPOTENCY_TTL = 600.0
_IDEMPOTENCY_MAX = 256
_IDEMPOTENCY_RESULT_MAX_BYTES = 262144

# ── v6.5 · 最近写回执（内存环形表；完整回执仍随任务账本持久化）──
_RECEIPT_HISTORY = []
_RECEIPT_HISTORY_MAX = 200
_RECEIPT_LOCK = threading.Lock()

_LOCK_PATH_KEYS = ("bp_path", "blueprint_path", "asset_path", "widget_path",
                   "material_path", "instance_path")


def _finite_float(value, default: float, lo: float, hi: float) -> float:
    """有限浮点夹取：NaN/Inf/非法值回默认值（防 ttl/超时被 NaN 绕过）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return max(lo, min(v, hi))


def _norm_lock_key(value) -> str:
    """锁键归一化：统一斜杠、去尾部斜杠、小写（UE 资产路径大小写不敏感）。"""
    return str(value or "").strip().replace("\\", "/").rstrip("/").lower()


def _lock_owner() -> str:
    try:
        if _TASK_JOURNAL_AVAILABLE and _task_journal is not None:
            tid = _task_journal.active_task_id()
            if tid:
                return tid
    except Exception:
        pass
    return "adhoc"


def _prune_locks(now: float = None) -> None:
    now = now or time.time()
    with _LOCKS_LOCK:
        for key in [k for k, v in _ASSET_LOCKS.items()
                    if v.get("expires", 0) <= now]:
            _ASSET_LOCKS.pop(key, None)


def _prune_session_locks_locked(now: float) -> None:
    for key in [k for k, v in _SESSION_LOCKS.items()
                if float(v.get("expires_at") or 0) <= now]:
        _SESSION_LOCKS.pop(key, None)


def _find_asset_lock_locked(asset: str):
    """返回 (现有 key, lock dict)；大小写不敏感匹配同一条目。"""
    if asset in _ASSET_LOCKS:
        return asset, _ASSET_LOCKS[asset]
    key = _norm_lock_key(asset)
    for k, v in _ASSET_LOCKS.items():
        if _norm_lock_key(k) == key:
            return k, v
    return None, None


def _asset_lock_acquire(asset: str, owner: str = "") -> None:
    """同一资产同一时刻只允许一个 owner（任务/会话）写；TTL 自动过期。"""
    asset = _safe_game_pkg(asset)
    if not asset:
        return
    owner = owner or _lock_owner()
    now = time.time()
    with _LOCKS_LOCK:
        _prune_locks(now)
        _prune_session_locks_locked(now)
        cur_key, cur = _find_asset_lock_locked(asset)
        if cur and cur.get("owner") != owner:
            raise BridgeError(
                "资产被另一个任务锁定：%s（owner=%s，剩余 %.0fs）"
                % (asset, cur.get("owner"),
                   max(0, cur.get("expires", 0) - now)),
                category="asset_locked",
                suggestion="等对方 task_end，或换资产；UE5_ASSET_LOCK_TTL 可调租约")
        sl = _SESSION_LOCKS.get(_norm_lock_key(asset))
        if sl and sl.get("owner") != owner:
            raise BridgeError(
                "资产被另一个会话锁定：%s（owner=%s，剩余 %.0fs）"
                % (asset, sl.get("owner"),
                   max(0.0, float(sl.get("expires_at") or 0) - now)),
                category="asset_locked",
                suggestion="等对方 session_lock_release/过期，或换资产")
        _ASSET_LOCKS[asset] = {"owner": owner, "ts": now,
                               "expires": now + _ASSET_LOCK_TTL}


def _asset_lock_release(asset: str, owner: str) -> None:
    with _LOCKS_LOCK:
        key, cur = _find_asset_lock_locked(str(asset or ""))
        if cur and cur.get("owner") == owner:
            _ASSET_LOCKS.pop(key, None)


def _release_locks(owner: str = "") -> int:
    owner = owner or _lock_owner()
    with _LOCKS_LOCK:
        keys = [k for k, v in _ASSET_LOCKS.items() if v.get("owner") == owner]
        for k in keys:
            _ASSET_LOCKS.pop(k, None)
        return len(keys)


def _locks_snapshot() -> list:
    now = time.time()
    with _LOCKS_LOCK:
        _prune_locks(now)
        _prune_session_locks_locked(now)
        out = [{"asset": k, "owner": v.get("owner"),
                "scope": "task",
                "expires_in": round(max(0.0, v.get("expires", 0) - now))}
               for k, v in _ASSET_LOCKS.items()]
        out.extend(
            {"asset": v.get("path") or k, "owner": v.get("owner"),
             "scope": "session",
             "expires_in": round(max(0.0, float(v.get("expires_at") or 0) - now))}
            for k, v in _SESSION_LOCKS.items())
        return out


def _cmd_session_lock_acquire(owner: str, assets=None, ttl: float = 600,
                              force: bool = False) -> dict:
    """抢显式资产锁（owner 必填；assets 为路径或路径数组；force 可抢占）。"""
    owner = str(owner or "").strip()
    if not owner:
        raise BridgeError("session_lock_acquire 需要 owner（会话/任务标识）",
                          category="param_type")
    if len(owner) > 128:
        raise BridgeError("owner 过长（上限 128 字符）", category="param_type")
    if isinstance(assets, str):
        assets = [assets]
    if not isinstance(assets, list) or not assets:
        raise BridgeError("assets 必须为非空数组（资产路径列表）",
                          category="param_type")
    if len(assets) > _SESSION_LOCK_ASSETS_MAX:
        raise BridgeError("assets 超过上限 %d（分批 acquire）"
                          % _SESSION_LOCK_ASSETS_MAX, category="param_type")
    ttl = _finite_float(ttl, _SESSION_LOCK_TTL_DEFAULT, 5.0,
                        _SESSION_LOCK_TTL_MAX)
    key_paths = {}
    for raw in assets:
        pkg = _safe_game_pkg(raw)
        if not pkg:
            raise BridgeError("资产路径非法（只允许 /Game/ 下路径）: %r" % (raw,),
                              category="param_type")
        key = _norm_lock_key(pkg)
        if len(key) > _SESSION_LOCK_KEY_MAX:
            raise BridgeError("资产路径过长（上限 %d 字符）"
                              % _SESSION_LOCK_KEY_MAX, category="param_type")
        key_paths.setdefault(key, pkg)
    keys = list(key_paths)
    if not keys:
        raise BridgeError("assets 中没有有效资产路径", category="param_type")
    now = time.time()
    acquired, stolen = [], []
    with _LOCKS_LOCK:
        _prune_locks(now)
        _prune_session_locks_locked(now)
        owner_count = sum(1 for v in _SESSION_LOCKS.values()
                          if v.get("owner") == owner)
        owner_new = [k for k in keys
                     if k not in _SESSION_LOCKS
                     or _SESSION_LOCKS[k].get("owner") != owner]
        global_new = [k for k in keys if k not in _SESSION_LOCKS]
        if owner_count + len(owner_new) > _SESSION_LOCK_MAX_PER_OWNER:
            raise BridgeError(
                "会话锁数量超过单 owner 上限 %d（先 release 或缩短 ttl）"
                % _SESSION_LOCK_MAX_PER_OWNER, category="session_lock")
        if len(_SESSION_LOCKS) + len(global_new) > _SESSION_LOCK_MAX:
            raise BridgeError(
                "会话锁总数超过上限 %d（先 release 或等过期）"
                % _SESSION_LOCK_MAX, category="session_lock")
        blocked, task_blocked = [], []
        for key in keys:
            cur = _SESSION_LOCKS.get(key)
            if cur and cur.get("owner") != owner:
                blocked.append({
                    "asset": cur.get("path") or key,
                    "owner": cur.get("owner"),
                    "remaining_s": round(max(
                        0.0, float(cur.get("expires_at") or 0) - now), 1),
                })
            for tk, tv in _ASSET_LOCKS.items():
                if _norm_lock_key(tk) == key and tv.get("owner") != owner:
                    task_blocked.append({
                        "asset": tk, "owner": tv.get("owner"),
                        "remaining_s": round(max(
                            0.0, float(tv.get("expires") or 0) - now), 1),
                    })
        if task_blocked:
            raise BridgeError(
                "资产被任务锁定（任务锁不能被 force 抢占）：" + "; ".join(
                    "%s by %s (剩余 %.0fs)"
                    % (b["asset"], b["owner"], b["remaining_s"])
                    for b in task_blocked)
                + " —— 等对方 task_end 或换资产", category="session_lock")
        if blocked and not force:
            raise BridgeError(
                "资产被其他会话锁定：" + "; ".join(
                    "%s by %s (剩余 %.0fs)"
                    % (b["asset"], b["owner"], b["remaining_s"])
                    for b in blocked)
                + " —— 等对方释放/过期，或显式 force=true 抢占",
                category="session_lock")
        for key in keys:
            cur = _SESSION_LOCKS.get(key)
            if cur and cur.get("owner") != owner:
                stolen.append({"asset": cur.get("path") or key,
                               "from": cur.get("owner")})
            _SESSION_LOCKS[key] = {
                "owner": owner, "path": key_paths[key],
                "acquired_at": now, "expires_at": now + ttl,
            }
            acquired.append(key)
    out = {"success": True, "owner": owner, "acquired": acquired,
           "ttl": ttl, "stolen": stolen}
    if stolen:
        out["warning"] = "force=true 抢占了其他会话的锁"
    return out


def _cmd_session_lock_release(owner: str, assets=None) -> dict:
    """释放 owner 的显式会话锁；assets 省略 = 释放该 owner 的全部会话锁。"""
    owner = str(owner or "").strip()
    if not owner:
        raise BridgeError("session_lock_release 需要 owner", category="param_type")
    if len(owner) > 128:
        raise BridgeError("owner 过长（上限 128 字符）", category="param_type")
    now = time.time()
    with _LOCKS_LOCK:
        _prune_session_locks_locked(now)
        if assets:
            raw_list = assets if isinstance(assets, list) else [assets]
            keys = [_norm_lock_key(a) for a in raw_list]
        else:
            keys = [k for k, v in _SESSION_LOCKS.items()
                    if v.get("owner") == owner]
        released = []
        for key in keys:
            cur = _SESSION_LOCKS.get(key)
            if cur and cur.get("owner") == owner:
                _SESSION_LOCKS.pop(key, None)
                released.append(cur.get("path") or key)
    return {"success": True, "owner": owner, "released": released,
            "count": len(released)}


def _cmd_session_lock_status() -> dict:
    """查看显式会话锁（过期项自动清理；任务锁见 /ui 的 locks 字段）。"""
    now = time.time()
    with _LOCKS_LOCK:
        _prune_session_locks_locked(now)
        locks = [{
            "asset": v.get("path") or k,
            "owner": v.get("owner"),
            "remaining_s": round(max(
                0.0, float(v.get("expires_at") or 0) - now), 1),
        } for k, v in sorted(_SESSION_LOCKS.items())]
    return {"success": True, "count": len(locks), "locks": locks}


def _lock_targets(cmd: str, params: dict) -> List[str]:
    """从写命令参数提取目标资产（归一化 key），用于协作锁检查。"""
    params = params if isinstance(params, dict) else {}
    out: List[str] = []

    def _add(value):
        pkg = _safe_game_pkg(value)
        key = _norm_lock_key(pkg)
        if key and key not in out:
            out.append(key)

    for k in _LOCK_PATH_KEYS:
        if params.get(k):
            _add(params[k])
    if cmd == "build_blueprint":
        name = params.get("bp_name") or params.get("blueprint_name")
        if name:
            pkg = str(params.get("pkg") or params.get("package_path")
                      or "/Game/Blueprints/").rstrip("/")
            _add(pkg + "/" + str(name))
    if cmd == "batch_asset_ops" and isinstance(params.get("items"), list):
        for it in params["items"]:
            if isinstance(it, dict):
                for key in ("from", "to"):
                    if it.get(key):
                        _add(it.get(key))
    return out


def _ensure_session_locks_free(owner: str, cmd: str, params: dict) -> None:
    """带 session_id 的写命令执行前检查会话锁 + 任务锁（协作式）。"""
    targets = _lock_targets(cmd, params)
    if not targets:
        return
    now = time.time()
    with _LOCKS_LOCK:
        _prune_locks(now)
        _prune_session_locks_locked(now)
        blocked = []
        for key in targets:
            cur = _SESSION_LOCKS.get(key)
            if cur and cur.get("owner") != owner:
                blocked.append((cur.get("path") or key, cur.get("owner"),
                                round(max(0.0, float(cur.get("expires_at") or 0)
                                          - now), 1)))
            for tk, tv in _ASSET_LOCKS.items():
                if _norm_lock_key(tk) == key and tv.get("owner") != owner:
                    blocked.append((tk, tv.get("owner"),
                                    round(max(0.0, float(tv.get("expires") or 0)
                                              - now), 1)))
    if blocked:
        raise BridgeError(
            "写目标被其他会话锁定：" + "; ".join(
                "%s by %s (剩余 %.0fs)" % b for b in blocked)
            + " —— 先 session_lock_release/等过期，或换目标",
            category="session_lock")


def _idem_fingerprint(cmd: str, params: dict) -> str:
    """命令+参数的稳定指纹；防止同一个 key 被复用到别的操作上。"""
    try:
        payload = json.dumps({"command": cmd, "params": params or {}},
                             ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        payload = repr((cmd, params))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _idem_get(key: str, cmd: str = "", params: dict = None):
    if not key:
        return None
    now = time.time()
    fp = _idem_fingerprint(cmd, params)
    with _IDEMPOTENCY_LOCK:
        ent = _IDEMPOTENCY.get(key)
        if not ent:
            return None
        if now - float(ent.get("ts") or 0) > _IDEMPOTENCY_TTL:
            _IDEMPOTENCY.pop(key, None)
            return None
        if ent.get("command") != cmd or ent.get("fingerprint") != fp:
            raise BridgeError(
                "idempotency_key 已被用于不同的命令/参数（key=%r）；"
                "请换 key，或用完全相同的 command+params 重试" % key,
                category="idempotency_conflict")
        return copy.deepcopy(ent.get("result"))


def _idem_put(key: str, cmd: str, params: dict, result) -> None:
    if not key or not isinstance(result, dict):
        return
    try:
        blob = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        return
    if len(blob.encode("utf-8")) > _IDEMPOTENCY_RESULT_MAX_BYTES:
        return
    with _IDEMPOTENCY_LOCK:
        if len(_IDEMPOTENCY) >= _IDEMPOTENCY_MAX:
            oldest = min(_IDEMPOTENCY.items(),
                         key=lambda kv: float(kv[1].get("ts") or 0))[0]
            _IDEMPOTENCY.pop(oldest, None)
        _IDEMPOTENCY[key] = {
            "ts": time.time(), "command": cmd,
            "fingerprint": _idem_fingerprint(cmd, params),
            "result": copy.deepcopy(result),
        }


def _cmd_receipt_list(limit: int = 20) -> dict:
    """最近写命令回执（倒序；默认 20 条，上限 200）。"""
    try:
        n = max(1, min(int(limit), _RECEIPT_HISTORY_MAX))
    except (TypeError, ValueError):
        n = 20
    with _RECEIPT_LOCK:
        items = list(_RECEIPT_HISTORY[-n:])
    items.reverse()
    return {"success": True, "count": len(items), "receipts": items}


def _project_content_dir_cached() -> str:
    if _CONTENT_DIR_CACHE["path"]:
        return _CONTENT_DIR_CACHE["path"]
    try:
        p = _gt_run_handler(
            lambda: unreal.Paths.convert_relative_path_to_full(
                unreal.Paths.project_content_dir()), timeout=3.0)
        _CONTENT_DIR_CACHE["path"] = str(p or "")
    except Exception:
        _CONTENT_DIR_CACHE["path"] = ""
    return _CONTENT_DIR_CACHE["path"]


def _asset_stat(asset: str) -> dict:
    """资产文件指纹（sha1 + size）；文件不存在/非 /Game/ → exists=false。"""
    pkg = _safe_game_pkg(asset)
    if not pkg:
        return {"exists": False, "error": "unsafe_path"}
    content = _project_content_dir_cached()
    if not content:
        return {"exists": False, "error": "content_dir_unavailable"}
    rel = pkg[len("/Game/"):]
    for ext in (".uasset", ".umap"):
        path = os.path.join(str(content), rel.replace("/", os.sep) + ext)
        if os.path.isfile(path):
            st = os.stat(path)
            sha = ""
            if st.st_size <= 100 * 1024 * 1024:
                h = hashlib.sha1()
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                sha = h.hexdigest()
            return {"exists": True, "size": st.st_size, "sha1": sha}
    return {"exists": False}


def _receipt_asset(cmd: str, params: dict) -> str:
    params = params if isinstance(params, dict) else {}
    for key in ("bp_path", "asset_path", "blueprint_path", "widget_path",
                "material_path", "instance_path"):
        if params.get(key):
            return str(params[key])
    if cmd == "build_blueprint":
        pkg = str(params.get("pkg") or "/Game/Blueprints/").rstrip("/")
        name = str(params.get("bp_name") or params.get("blueprint_name") or "")
        return pkg + "/" + name if name else ""
    return ""


#: 需要回执的命令（写操作）
_RECEIPT_COMMANDS = frozenset(
    set(_TRANSACTIONAL_COMMANDS) | {
        "apply_graph", "build_blueprint", "delete_asset", "delete_node",
        "batch_asset_ops", "datatable_write_csv", "datatable_create", "save_asset",
        "material_create", "widget_create",
    })


def _receipt_begin(cmd: str, params: dict) -> dict:
    if cmd not in _RECEIPT_COMMANDS:
        return {}
    asset = _receipt_asset(cmd, params)
    ctx = {"id": hashlib.sha1(
        ("%s|%s|%s" % (time.time(), cmd, asset)).encode("utf-8")).hexdigest()[:10],
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "command": cmd,
        "asset": asset, "before": _asset_stat(asset) if asset else {"exists": False}}
    ctx["owner"] = _lock_owner()
    # adhoc（无任务）不加锁：单进程内由 GT 串行化保证；锁只服务多任务协作
    if ctx["owner"] != "adhoc":
        assets = [asset] if asset else []
        if cmd == "batch_asset_ops" and isinstance(params.get("items"), list):
            for it in params["items"]:
                if isinstance(it, dict):
                    for key in ("from", "to"):
                        p = _safe_game_pkg(it.get(key))
                        if p and p not in assets:
                            assets.append(p)
        for a in assets:
            _asset_lock_acquire(a, ctx["owner"])
        ctx["assets"] = assets
    return ctx


def _receipt_finish(ctx: dict, result) -> dict:
    if not ctx:
        return result
    res = result if isinstance(result, dict) else {}
    asset = ctx.get("asset") or ""
    after = _asset_stat(asset) if asset else {"exists": False}
    before = ctx.get("before") or {"exists": False}
    receipt = {
        "id": ctx["id"], "receipt_id": "rcpt_" + str(ctx["id"]),
        "ts": ctx["ts"], "command": ctx["command"],
        "asset": asset,
        "before": {k: before.get(k) for k in ("exists", "size", "sha1")},
        "after": {k: after.get(k) for k in ("exists", "size", "sha1")},
        "saved": bool(before.get("sha1") != after.get("sha1")),
        "success": bool(res.get("success", res.get("ok", False))),
        "verified": res.get("verified") if "verified" in res else None,
        "idempotent_replay": bool(res.get("idempotent_replay")),
    }
    for key in ("nodes_applied", "edges_applied", "rows_after", "renamed",
                "deleted", "compile_status"):
        if key in res:
            v = res[key]
            receipt[key] = len(v) if isinstance(v, list) else v
    if not receipt["saved"] and asset:
        receipt["note"] = ("幂等重放：未重复执行写操作" if receipt["idempotent_replay"]
                           else "文件未变化（可能只在内存中，task_end 会自动保存）")
    # 锁由 task_end 统一释放（ctx.owner 为任务 id）；adhoc 不加锁
    if isinstance(result, dict):
        result["receipt"] = receipt
    try:
        with _RECEIPT_LOCK:
            _RECEIPT_HISTORY.append(copy.deepcopy(receipt))
            if len(_RECEIPT_HISTORY) > _RECEIPT_HISTORY_MAX:
                del _RECEIPT_HISTORY[:len(_RECEIPT_HISTORY) - _RECEIPT_HISTORY_MAX]
    except Exception:
        pass
    try:
        if _TASK_JOURNAL_AVAILABLE and _task_journal is not None:
            _task_journal.note_receipt(receipt)
    except Exception:
        pass
    return result


def _dispatch_with_tx(cmd: str, fn, **params):
    """白名单命令统一入口：写命令 = 一个 UE 事务 = 一个撤销步。

    不可用 / 用户事务在场（Begin 返回 False）→ 原样执行：宁可不可撤销，
    也不能插队关掉用户自己的事务。嵌套调用由 C++ 深度计数合并成一步。
    """
    return _dispatch_args_with_tx(cmd, fn, **params)


def _dispatch_args_with_tx(cmd: str, fn, *args, **kwargs):
    """_dispatch_with_tx 的通用形态（MCP impl 是 impl(args) 单参形态）。

    v6.5 多 Agent：`session_id` 与 `idempotency_key` 由本层消费，不传给具体
    命令；写命令在 session_id 在场时先做协作锁检查；成功结果按幂等键缓存。
    ⚠️ 不要 pop `owner`：session_lock_* 自己的参数就叫 owner。
    """
    session = kwargs.pop("session_id", None)
    owner = str(session or "").strip()
    idem = str(kwargs.pop("idempotency_key", "") or "").strip()
    if len(owner) > 128:
        raise BridgeError("session_id 过长（上限 128 字符）", category="param_type")
    if len(idem) > 128:
        raise BridgeError("idempotency_key 过长（上限 128 字符）",
                          category="param_type")

    if idem:
        cached = _idem_get(idem, cmd, kwargs)
        if cached is not None:
            if isinstance(cached, dict):
                cached["idempotent_replay"] = True
            return cached

    if owner and cmd in _RECEIPT_COMMANDS:
        _ensure_session_locks_free(owner, cmd, kwargs)

    if (not _AGENT_TX_ENABLED or cmd not in _TRANSACTIONAL_COMMANDS
            or not _agent_tx_available()):
        result = fn(*args, **kwargs)
    elif not _agent_tx_begin(_agent_tx_title(cmd, kwargs)):
        result = fn(*args, **kwargs)
    else:
        try:
            result = fn(*args, **kwargs)
        finally:
            _agent_tx_end()

    if idem and isinstance(result, dict) \
            and result.get("success") is not False \
            and result.get("ok") is not False \
            and not result.get("_fail_loud"):
        _idem_put(idem, cmd, kwargs, result)
    return result


def _agent_tx_require() -> None:
    if not _agent_tx_available():
        raise BridgeError(
            "当前 bridge DLL 不含事务入口（需 v4.5 版）—— 重新编译并替换 DLL 后重启编辑器",
            category="bridge_unavailable",
            suggestion="bridge/BlueprintPythonBridge/Source/build_for_engine.bat")


def _cmd_agent_undo_status(timeout: float = 30.0) -> dict:
    """撤销状态：可回滚步数 + 下一步标题 + 是否 agent 步（面板/agent 共用）。"""
    _agent_tx_require()
    raw = _gt_run_handler(lambda: bridge.get_agent_undo_status(), timeout=timeout)
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except ValueError:
        raise BridgeError(f"撤销状态返回非法 JSON: {raw!r}", category="agent_undo")
    if not data.get("ok"):
        raise BridgeError(str(data.get("error") or "撤销状态查询失败"),
                          category="agent_undo")
    return data


def _cmd_agent_undo(count: int = 1) -> dict:
    """回滚最近 count 个 agent 事务（碰到用户操作立刻停，不跳过）。"""
    _agent_tx_require()
    try:
        n = int(count)
    except (TypeError, ValueError):
        raise BridgeError(f"count 必须为整数（收到 {count!r}）", category="param_type")
    if n < 1:
        raise BridgeError("count 至少为 1", category="param_type")
    if n > 500:
        raise BridgeError("count 上限 500（一次回滚太多步多半是误用）",
                          category="param_type")
    raw = _gt_run_handler(lambda: bridge.undo_agent_transactions(n), timeout=120.0)
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except ValueError:
        raise BridgeError(f"撤销返回非法 JSON: {raw!r}", category="agent_undo")
    if not data.get("ok"):
        raise BridgeError(str(data.get("error") or "撤销失败"), category="agent_undo")
    if int(data.get("undone") or 0) == 0 and data.get("stopped_reason"):
        data["_fail_loud"] = True
        data["error"] = str(data["stopped_reason"])
        data["success"] = False
    # 任务账本注记（面板"已回滚 N 步"的数据源；无任务在跑时静默跳过）
    try:
        if _TASK_JOURNAL_AVAILABLE:
            _task_journal.note_rollback(
                int(data.get("undone") or 0), data.get("titles") or [],
                str(data.get("stopped_reason") or ""))
    except Exception:
        pass
    data.setdefault("success", True)
    return data


#: 面板轮询用的撤销状态缓存（2s TTL）：编辑器忙着编译时 GT 排队，不能让
#: /ui/api/task 每次等满超时（面板会卡）；缓存 + 短超时保证观测面始终可用。
_UNDO_STATUS_CACHE = {"ts": 0.0, "data": None}


def _undo_status_cached(ttl: float = 2.0) -> dict:
    now = time.time()
    cached = _UNDO_STATUS_CACHE.get("data")
    if cached is not None and now - float(_UNDO_STATUS_CACHE.get("ts") or 0.0) < ttl:
        return cached
    try:
        if not _agent_tx_available():
            data = {"ok": False, "available": False,
                    "error": "当前 bridge DLL 无事务入口（需 v4.5 版）"}
        else:
            data = _cmd_agent_undo_status(timeout=2.0)
    except Exception as e:
        data = {"ok": False, "available": False, "error": str(e)[:300]}
    _UNDO_STATUS_CACHE["ts"] = time.time()
    _UNDO_STATUS_CACHE["data"] = data
    return data


_COMMAND_WHITELIST = {
    "health":            lambda **kw: _get_ue_info(),
    "list_assets":       lambda path="/Game/", recursive=True, **kw: _cmd_list_assets(path, recursive),
    "read_blueprint":    lambda bp_path, level="L1", include_cdo=True, include_conn=True, fields=None, **kw: _shape_fields(_cmd_read_blueprint(bp_path, level, include_cdo, include_conn), fields),
    "read_variables":    lambda bp_path, **kw: _cmd_read_variables(bp_path),
    "read_cdo":          lambda bp_path, **kw: _cmd_read_cdo(bp_path),
    "read_nodes":        lambda bp_path, graph_name=None, fields=None, **kw: _shape_fields(_cmd_read_nodes(bp_path, graph_name), fields),
    "read_connections":  lambda bp_path, graph_name="EventGraph", fields=None, **kw: _shape_fields(_cmd_read_connections(bp_path, graph_name), fields),
    "build_blueprint":   lambda bp_name, pkg="/Game/Blueprints/", parent="Actor", vars=None, comps=None, interfaces=None, compile_after=True, **kw: _cmd_build_blueprint(bp_name, pkg, parent, vars, comps, interfaces, compile_after),  # 兼容旧调用，新代码用 POST /build
    "add_variable":      lambda bp_path, var_name, var_type, default_value="", category="", expose=True, **kw: _cmd_add_variable(bp_path, var_name, var_type, default_value, category, expose),
    "add_component":     lambda bp_path, comp_name, comp_class, properties=None, **kw: _cmd_add_component(bp_path, comp_name, comp_class, properties),
    "add_function_node": lambda bp_path, graph_name, function_path, node_pos=None, **kw: _cmd_add_function_node(bp_path, graph_name, function_path, node_pos),
    "connect_pins":      lambda bp_path, connections, **kw: _cmd_connect_pins(bp_path, connections),
    "compile_blueprint": lambda bp_path, **kw: _cmd_compile_blueprint(bp_path),
    # ── 编译诊断（T-20261024-UE5BRIDGE-COMPILE-DIAGNOSE · 72→73 条）──
    #   编译 + 结构化错误抽取（asset/function/node_guid 定位）+ 规则库映射，
    #   修复决策仍交给模型（工具只提供可定位的错误，不搞启发式自动修）。
    "compile_and_diagnose": lambda bp_path, **kw: _cmd_compile_and_diagnose(bp_path),
    # ── 整图声明式写入（T-20261024-UE5BRIDGE-GRAPH-APPLY · 73→75 条）──
    #   "Never build a graph one node at a time"（UEBox 纪律）：查一批 →
    #   写一整张。search 只读（签名与 _read_l2_nodes 同源）；apply 是
    #   原子事务（守卫 → 建图连线 → 事务外编译 → 读回 verified → 失败回滚）。
    "search_blueprint_nodes": lambda bp_path, query="", limit=20, **kw: _cmd_search_blueprint_nodes(bp_path, query, limit),
    "apply_graph": lambda bp_path, graph_spec=None, replace=False, **kw: _cmd_apply_graph(bp_path, graph_spec, replace),
    # ── 截图反馈闭环（T-20261024-UE5BRIDGE-SCREENSHOT · 75→78 条）──
    #   给 agent 装上"眼睛"：修改 → 截图 → 视觉自检 → 修正。转发 C++ 桥
    #   capture_viewport / capture_pie / project_actors_to_screen（v3.7 离线草稿，
    #   回家编译 DLL 后可用；旧 DLL → 干净降级报错 fail-loud）。
    #   pending 截图（FScreenshotRequest 延迟一帧）由端点层 _capture_finish_pending
    #   在 HTTP 线程收尾 —— GT 上等文件 = 阻塞帧推进 = 自死锁。
    "capture_viewport":  lambda out_path="", camera_mode="active_viewport", pitch=0.0, yaw=0.0, roll=0.0, width=0, height=0, **kw: _cmd_capture_viewport(out_path, camera_mode, pitch, yaw, roll, width, height),
    "capture_pie":       lambda out_path="", width=0, height=0, **kw: _cmd_capture_pie(out_path, width, height),
    "project_actors_to_screen": lambda actor_names, camera_mode="active_viewport", pitch=0.0, yaw=0.0, roll=0.0, **kw: _cmd_project_actors_to_screen(actor_names, camera_mode, pitch, yaw, roll),
    # D-2/D-4（T-20260916-UE5AUTO-HALLBEACON-D1D4-FIX）：① 补默认值 → 缺参时走
    #   可读 BridgeError（而非裸 TypeError）；② `node_guid` 为 GUID 定位的显式别名
    #   （等价 `node_id`，二者取非空者）——同名节点场景唯一可靠锚点。
    "set_pin_default_value": lambda bp_path="", node_id="", pin_name="", new_value="", node_guid=None, **kw: _cmd_set_pin_default_value(bp_path, node_id or (node_guid or ""), pin_name, new_value),
    "get_pin_default_value": lambda bp_path="", node_id="", pin_name="", node_guid=None, **kw: _cmd_get_pin_default_value(bp_path, node_id or (node_guid or ""), pin_name),
    # ── PATCH-A/B 新接口 · L1 封装（T-20260910-UE5BRIDGE-PYWRAP-L1-IMPL · 11 条）──
    # ⚠️ 白名单 + handler **必须双侧同步**：漏一侧 → 调用返回 Unknown command。
    "create_node_by_class": lambda bp_path, graph_name="EventGraph", node_class="", node_pos=None, **kw: _cmd_create_node_by_class(bp_path, graph_name, node_class, node_pos),
    "set_node_position": lambda bp_path, graph_name="EventGraph", node_guid="", x=0, y=0, **kw: _cmd_set_node_position(bp_path, graph_name, node_guid, x, y),
    "get_node_by_guid": lambda bp_path, graph_name="EventGraph", node_guid="", **kw: _cmd_get_node_by_guid(bp_path, graph_name, node_guid),
    "add_variable_node": lambda bp_path, graph_name="EventGraph", var_name="", b_is_setter=True, node_pos=None, **kw: _cmd_add_variable_node(bp_path, graph_name, var_name, b_is_setter, node_pos),
    "add_custom_event_node": lambda bp_path, graph_name="EventGraph", event_name="", node_pos=None, signature_func="", **kw: _cmd_add_custom_event_node(bp_path, graph_name, event_name, node_pos, signature_func),
    "add_input_key_event_node": lambda bp_path, graph_name="EventGraph", key_name="", node_pos=None, **kw: _cmd_add_input_key_event_node(bp_path, graph_name, key_name, node_pos),
    "add_function_pin": lambda bp_path, graph_name="", pin_name="", pin_type="", b_is_input=True, **kw: _cmd_add_function_pin(bp_path, graph_name, pin_name, pin_type, b_is_input),
    "add_local_variable": lambda bp_path, graph_name="", var_name="", var_type="", **kw: _cmd_add_local_variable(bp_path, graph_name, var_name, var_type),
    "add_timeline_node": lambda bp_path, graph_name="EventGraph", timeline_name="", node_pos=None, strict_pins=True, **kw: _cmd_add_timeline_node(bp_path, graph_name, timeline_name, node_pos, strict_pins),
    "add_comment_node": lambda bp_path, graph_name="EventGraph", text="", node_pos=None, width=400, height=200, **kw: _cmd_add_comment_node(bp_path, graph_name, text, node_pos, width, height),
    "set_class_default": lambda bp_path, property_name="", value="", **kw: _cmd_set_class_default(bp_path, property_name, value),
    # ── PATCH-A/B 剩余接口 · L2 封装（T-20260912-UE5BRIDGE-PYWRAP-L2-IMPL · 21 条）──
    "add_pin_to_node": lambda bp_path, graph_name="EventGraph", node_guid="", direction="input", pin_type="", pin_name="", **kw: _cmd_add_pin_to_node(bp_path, graph_name, node_guid, direction, pin_type, pin_name),
    "remove_pin_from_node": lambda bp_path, graph_name="EventGraph", node_guid="", pin_name="", **kw: _cmd_remove_pin_from_node(bp_path, graph_name, node_guid, pin_name),
    "try_node_add_input_pin": lambda bp_path, graph_name="EventGraph", node_guid="", strict=True, **kw: _cmd_try_node_add_input_pin(bp_path, graph_name, node_guid, strict),
    "set_node_property": lambda bp_path, graph_name="EventGraph", node_guid="", property_name="", value="", **kw: _cmd_set_node_property(bp_path, graph_name, node_guid, property_name, value),
    "add_cast_node": lambda bp_path, graph_name="EventGraph", target_class_name="", node_pos=None, **kw: _cmd_add_cast_node(bp_path, graph_name, target_class_name, node_pos),
    "add_class_cast_node": lambda bp_path, graph_name="EventGraph", target_class_name="", node_pos=None, **kw: _cmd_add_class_cast_node(bp_path, graph_name, target_class_name, node_pos),
    "add_macro_node": lambda bp_path, graph_name="EventGraph", macro_name="", node_pos=None, **kw: _cmd_add_macro_node(bp_path, graph_name, macro_name, node_pos),
    "add_switch_node": lambda bp_path, graph_name="EventGraph", type_kind="", enum_or_type_name="", node_pos=None, **kw: _cmd_add_switch_node(bp_path, graph_name, type_kind, enum_or_type_name, node_pos),
    "add_struct_node": lambda bp_path, graph_name="EventGraph", struct_name="", b_is_break=False, node_pos=None, **kw: _cmd_add_struct_node(bp_path, graph_name, struct_name, b_is_break, node_pos),
    "add_enum_literal_node": lambda bp_path, graph_name="EventGraph", enum_name="", value_name="", node_pos=None, **kw: _cmd_add_enum_literal_node(bp_path, graph_name, enum_name, value_name, node_pos),
    "add_composite_node": lambda bp_path, graph_name="EventGraph", node_pos=None, **kw: _cmd_add_composite_node(bp_path, graph_name, node_pos),
    "add_math_expression_node": lambda bp_path, graph_name="EventGraph", expression="", node_pos=None, **kw: _cmd_add_math_expression_node(bp_path, graph_name, expression, node_pos),
    "add_async_action_node": lambda bp_path, graph_name="EventGraph", factory_function_name="", factory_class_name="", activate_function_name="", node_pos=None, **kw: _cmd_add_async_action_node(bp_path, graph_name, factory_function_name, factory_class_name, activate_function_name, node_pos),
    "add_create_delegate_node": lambda bp_path, graph_name="EventGraph", function_name="", node_pos=None, **kw: _cmd_add_create_delegate_node(bp_path, graph_name, function_name, node_pos),
    "add_actor_bound_event_node": lambda bp_path, graph_name="EventGraph", actor_name="", delegate_name="", node_pos=None, **kw: _cmd_add_actor_bound_event_node(bp_path, graph_name, actor_name, delegate_name, node_pos),
    "add_input_axis_event_node": lambda bp_path, graph_name="EventGraph", axis_name="", b_is_key_axis=False, node_pos=None, **kw: _cmd_add_input_axis_event_node(bp_path, graph_name, axis_name, b_is_key_axis, node_pos),
    "add_input_action_event_node": lambda bp_path, graph_name="EventGraph", action_name="", node_pos=None, **kw: _cmd_add_input_action_event_node(bp_path, graph_name, action_name, node_pos),
    "add_component_bound_event_node": lambda bp_path, graph_name="EventGraph", component_name="", delegate_name="", node_pos=None, **kw: _cmd_add_component_bound_event_node(bp_path, graph_name, component_name, delegate_name, node_pos),
    "remove_component": lambda bp_path, comp_name="", **kw: _cmd_remove_component(bp_path, comp_name),
    "add_implemented_interface": lambda bp_path, interface_class_path="", **kw: _cmd_add_implemented_interface(bp_path, interface_class_path),
    "add_event_dispatcher": lambda bp_path, delegate_name="", **kw: _cmd_add_event_dispatcher(bp_path, delegate_name),
    # ── 落盘（T-20260913-UE5BRIDGE-SAVE-ASSET · 47→48 条）──
    "save_asset": lambda asset_path, only_if_is_dirty=False, **kw: _cmd_save_asset(asset_path, only_if_is_dirty),
    # ── 删除（T-20260913-UE5BRIDGE-DELETE-COMPILE-FIX · D-1 · 48→49 条 · fail-loud）──
    "delete_asset": lambda asset_path, max_attempts=3, **kw: _cmd_delete_asset(asset_path, max_attempts),
    # ── 修法3（T-20260914-UE5BRIDGE-BUILD-HOLD-FIX · 49→50 条 · audit trail）──
    #   解绑 PyConsoleGlobalDict 中指向目标资产的 UObject 包装器 + collect_garbage；
    #   严格限定目标路径，绝不盲删 globals()。
    # D-4 修正（T-20260916-UE5AUTO-HALLBEACON-D1D4-FIX）：`asset_path` 补默认值
    #   `""` → 无参调用落到 `_cmd_unpin_console_refs` 的**可读错误**
    #   （success=false + 「asset_path 为空…」），而不再抛裸
    #   `TypeError: <lambda>() missing 1 required positional argument: 'asset_path'`。
    "unpin_console_refs": lambda asset_path="", **kw: _cmd_unpin_console_refs(asset_path),
    # ── v3.0 新能力族（T-20260918-V3-MCP-FUSION · 50→59 条）──
    # ① PIE 运行时验证（官方 MCP 的"读运行时状态证明行为"范式）
    "pie_start":         lambda mode="simulate", **kw: _cmd_pie_start(mode),
    "pie_stop":          lambda **kw: _cmd_pie_stop(),
    "pie_state":         lambda **kw: _cmd_pie_state(),
    # ── 任务日志（仪表盘 v2 数据脊柱 · T-20261024-UI-V2 · 纯主机侧记账，
    #    零 UE 依赖零 GT —— agent 上报意图/计划/进度/复盘，面板渲染脉络）──
    "task_begin":        lambda **kw: _cmd_task_begin(kw),
    "task_step":         lambda **kw: _cmd_task_step(kw),
    "task_end":          lambda **kw: _cmd_task_end(kw),
    "task_list":         lambda **kw: _cmd_task_list(),
    "task_error":        lambda **kw: _cmd_task_error(kw),
    # ── 任务级撤销（T-20261027-UE5BRIDGE-TASK-UNDO · 84→86 条）──
    #   写命令自动各留一步带 [agent] 前缀的 UE 事务；回滚只撤这些步，
    #   遇用户操作即停（最多 count 步）。需 v4.5 版 DLL。
    "agent_undo_status": lambda **kw: _cmd_agent_undo_status(),
    "agent_undo":        lambda count=1, **kw: _cmd_agent_undo(count),
    # ── v6.5 多 Agent 协作：显式会话锁 + 回执查询（纯主机侧，零 UE 依赖）──
    #   任务锁（task_*）自动持有；会话锁给不建 task 的客户端显式协作。
    "session_lock_acquire": lambda owner, assets=None, ttl=600, force=False, **kw: _cmd_session_lock_acquire(owner, assets, ttl, force),
    "session_lock_release": lambda owner, assets=None, **kw: _cmd_session_lock_release(owner, assets),
    "session_lock_status": lambda **kw: _cmd_session_lock_status(),
    "receipt_list":       lambda limit=20, **kw: _cmd_receipt_list(limit),
    # ── v6.5 AnimBP 只读通道（probe/read；写入通道未验证，不开放）──
    "animbp_probe":       lambda asset_path, **kw: _cmd_animbp_probe(asset_path),
    "animbp_read":        lambda asset_path, include_nodes=True, limit=500, **kw: _cmd_animbp_read(asset_path, include_nodes, limit),
    # ── 反射探针（T-20261025-VERIFY · 只读：排查 UPROPERTY 随引擎的读法差异，
    #    bBeingCompiled 守卫降级排查首用）──
    "debug_reflect":     lambda asset_path="", probe="", **kw: _cmd_debug_reflect(asset_path, probe),
    # ② Actor 基础族（world="editor"|"pie"；PIE 中即运行时对象）
    "find_actors":       lambda actor_class="", name_filter="", world="editor", limit=100, **kw: _cmd_find_actors(actor_class, name_filter, world, limit),
    "spawn_actor":       lambda actor_class, location=None, rotation=None, world="editor", mobility="", **kw: _cmd_spawn_actor(actor_class, location, rotation, world, mobility),
    "set_actor_transform": lambda actor_name, location=None, rotation=None, world="editor", **kw: _cmd_set_actor_transform(actor_name, location, rotation, world),
    "get_actor_properties": lambda actor_name, property_name="", world="editor", limit=200, **kw: _cmd_get_actor_properties(actor_name, property_name, world, limit),
    # 行为验证（v4.9）：属性断言 + 截图证据 —— 补"编译通过 ≠ 行为正确"的语义验证
    "verify_behavior":   lambda actor_name, property_name, expected="", world="pie", match="exact", tolerance=0.0, capture=True, out_path="", **kw: _cmd_verify_behavior(actor_name, property_name, expected, world, match, tolerance, capture, out_path),
    # 资产治理（v5.0）：批量重命名/搬迁（dry-run 默认）+ 任务级资产回滚
    "batch_asset_ops":   lambda items, dry_run=True, confirm=False, stop_on_error=True, **kw: _cmd_batch_asset_ops(items, dry_run, confirm, stop_on_error),
    # 能力探针（v6.0 · 只读）：确认某类/工厂是否暴露（AnimBP/DataTable 排查用）
    "class_probe":       lambda names, include_members=False, **kw: _cmd_class_probe(names, include_members),
    # DataTable（v6.0）：读 / CSV 覆盖写 / 新建（行结构指定）
    "datatable_read":    lambda asset_path, limit=50, include_csv=True, **kw: _cmd_datatable_read(asset_path, limit, include_csv),
    "datatable_write_csv": lambda asset_path, csv, confirm=False, **kw: _cmd_datatable_write_csv(asset_path, csv, confirm),
    "datatable_create":  lambda asset_path, row_struct, package_path="", **kw: _cmd_datatable_create(asset_path, row_struct, package_path),
    # 场景化验收（v6.2 · 独家）：PIE 场景 + 断言 + 截图 + JUnit
    "verify_scenario":   lambda name, steps, start_pie=True, pie_mode="simulate", stop_pie=True, junit_path="", stop_on_failure=True, timeout_s=300.0, allow_destructive=False, **kw: _cmd_verify_scenario(name, steps, start_pie, pie_mode, stop_pie, junit_path, stop_on_failure, timeout_s, allow_destructive),
    "task_assets":       lambda task_id="", **kw: _cmd_task_assets(task_id),
    "rollback_task_assets": lambda task_id="", confirm=False, **kw: _cmd_rollback_task_assets(task_id, confirm),
    # ③ 日志查询（游标增量；诊断模式从"用户贴日志"升级为"Agent 自取日志"）
    "logs_read":         lambda lines=200, category="", pattern="", cursor=0, max_bytes=524288, log_file="", **kw: _cmd_logs_read(lines, category, pattern, cursor, max_bytes, log_file),
    # ④ 批量执行（白名单命令序列单次请求；多步操作压成一次往返）
    "editor_batch":      lambda steps, stop_on_error=True, **kw: _cmd_editor_batch(steps, stop_on_error),
    # ── v3.0 材质族（T-20260918-V3-MAT · 探针实测 MaterialEditingLibrary 全暴露）──
    "material_create":   lambda material_path, package_path="", **kw: _cmd_material_create(material_path, package_path),
    "material_add_expression": lambda material_path, expression_class, node_pos=None, **kw: _cmd_material_add_expression(material_path, expression_class, node_pos),
    "material_connect_expressions": lambda material_path, from_name, to_name, from_output="", to_input="", **kw: _cmd_material_connect_expressions(material_path, from_name, to_name, from_output, to_input),
    "material_connect_property": lambda material_path, from_name, property_name, from_output="", **kw: _cmd_material_connect_property(material_path, from_name, property_name, from_output),
    "material_set_expression_property": lambda material_path, expr_name, property_name, value=None, **kw: _cmd_material_set_expression_property(material_path, expr_name, property_name, value),
    "material_compile":  lambda material_path, **kw: _cmd_material_compile(material_path),
    "material_read":     lambda material_path, **kw: _cmd_material_read(material_path),
    "material_instance_create": lambda material_path, instance_path, **kw: _cmd_material_instance_create(material_path, instance_path),
    "material_instance_set_parameter": lambda instance_path, parameter_name, kind="scalar", value=None, **kw: _cmd_material_instance_set_parameter(instance_path, parameter_name, kind, value),
    # ── v3.0 UMG 控件族（T-20260918-V3-UMG · 需 v3.0 版 bridge DLL）──
    "widget_create":     lambda widget_path, package_path="", **kw: _cmd_widget_create(widget_path, package_path),
    "widget_add_child":  lambda widget_path, widget_class, widget_name, parent_name="", **kw: _cmd_widget_add_child(widget_path, widget_class, widget_name, parent_name),
    "widget_set_property": lambda widget_path, widget_name, property_name, value="", **kw: _cmd_widget_set_property(widget_path, widget_name, property_name, value),
    "widget_read_tree":  lambda widget_path, **kw: _cmd_widget_read_tree(widget_path),
}


def _gt_run_handler(fn, *args, timeout: float = 600.0, **kwargs):
    """端点处理器统一 GameThread 分流（v3.2 功能实测修复）。

    5.1 时代的内嵌 Python 对非 GT 访问宽容；5.8 起**引擎与桥 DLL 均直接拒绝**
    （"Attempted to access Unreal API from outside the main game thread"）。
    故所有可能触达 unreal/DLL 的端点处理器统一经此分流：

      · TEST_MODE / worker 未运行（UE 外、离线测试）→ 直呼；
      · 已在 GT 任务内（嵌套调用，如 editor_batch → 子命令）→ 直呼（防自等待死锁）；
      · 其余（HTTP 线程）→ 投递 python 侧 GT 队列同步执行（fail-loud 超时）。
    """
    if _TEST_MODE or _GT_WORKER_STOPPED or _GT_TASK_RUNNING:
        return fn(*args, **kwargs)
    return _run_on_game_thread_sync(fn, *args, timeout=timeout, **kwargs)


@app.post("/command", response_model=CommandResponse)
def execute_command(req: CommandRequest, request: Request = None):  # 🔧 B: async→sync def（T-20260806-UE5BRIDGE-CRASH-FIX 方案B·线程池隔离）
    """白名单命令模式 — 客户端只能说预定义命令。

    🔒 A-003 + A-004 安全升级：
      - 替代旧 /execute 沙箱端点
      - 只允许白名单命令，杜绝任意代码执行
      - 需 X-Skill-Token 头认证

    可用命令（101 条 = 原 15 + T-20260910-UE5BRIDGE-PYWRAP-L1-IMPL 新增 11 + T-20260912-UE5BRIDGE-PYWRAP-L2-IMPL 新增 21 + T-20260913-UE5BRIDGE-SAVE-ASSET 新增 1 + T-20260913-UE5BRIDGE-DELETE-COMPILE-FIX 新增 1 + T-20260914-UE5BRIDGE-BUILD-HOLD-FIX 新增 1 + T-20260918-V3-MCP-FUSION 新增 9 + T-20260918-V3-MAT 新增 9 + T-20260918-V3-UMG 新增 4 + T-20261024-UE5BRIDGE-COMPILE-DIAGNOSE 新增 1 + T-20261024-UE5BRIDGE-GRAPH-APPLY 新增 2 + T-20261024-UE5BRIDGE-SCREENSHOT 新增 3 + T-20261024-UI-V2 新增 5 + T-20261025-VERIFY 新增 1 + T-20261027-TASK-UNDO 新增 2 + T-20261028-VERIFY-BEHAVIOR 新增 1 + T-20261028-ASSET-GOVERN 新增 3 + T-20261028-CLASS-PROBE 新增 1 + T-20261028-DATATABLE 新增 3 + T-20261028-VERIFY-SCENARIO 新增 1 + v6.5 多 Agent/AnimBP 新增 6）:
        health, list_assets, read_blueprint, read_variables, read_cdo,
        read_nodes, read_connections, build_blueprint(兼容), add_variable,
        add_component, add_function_node, connect_pins, compile_blueprint,
        compile_and_diagnose(编译+结构化错误诊断 · asset/function/node_guid 定位),
        search_blueprint_nodes(只读 · 查函数/事件/流程控制候选+完整引脚签名,
            写 apply_graph 前先调它拿准确签名),
        apply_graph(声明式整图写入 · 原子事务+读回验证 verified+失败回滚),
        set_pin_default_value, get_pin_default_value

        ── 截图反馈闭环（T-20261024-UE5BRIDGE-SCREENSHOT · 需 v3.7 版 DLL）──
        capture_viewport(editor 视口机位 / agent 确定性机位; PIE 中自动转游戏画面) ·
        capture_pie(截 PIE 运行画面 · PIE 未运行 fail-loud) ·
        project_actors_to_screen(世界坐标→屏幕像素, 标注数据源)

        ── 任务日志（T-20261024-UI-V2 · 纯主机侧记账，零 UE/GT 依赖）──
        task_begin(声明任务: goal 必填 + understanding/approach/steps 计划脉络) ·
        task_step(step_id 定位计划步骤 status=running|done|failed|skipped 或 note 注记) ·
        task_end(status=success|failed|aborted + summary/lessons 复盘) ·
        task_list(全量任务) · task_error(错误注记挂当前任务)

        ── 任务级撤销（T-20261027-TASK-UNDO · 需 v4.5 版 DLL）──
        agent_undo_status(可回滚步数+下一步标题; next_is_agent=能否安全回滚) ·
        agent_undo(count=1, 连续撤销 agent 事务; 遇用户操作立即停, 不跳过)
        —— 写命令自动各留一步带 "[agent] " 前缀的 UE 事务，无需手动 begin/end。

        ── PATCH-A/B 新接口 L1 封装（均 fail-loud：底层 None/False → success=false）──
        create_node_by_class(AddNodeByClass) · set_node_position(SetNodePosition) ·
        get_node_by_guid(GetNodeByGuid) · add_variable_node(AddVariableNode) ·
        add_custom_event_node(AddCustomEventNode) ·
        add_input_key_event_node(AddInputKeyEventNode) ·
        add_function_pin(AddFunctionPinEntry) · add_local_variable(AddLocalVariable) ·
        add_timeline_node(AddTimelineNode) · add_comment_node(AddCommentNode) ·
        set_class_default(SetClassDefault)

        ── PATCH-A/B 剩余 21 项 · L2 封装（T-20260912-UE5BRIDGE-PYWRAP-L2-IMPL）──
        add_pin_to_node(AddPinToNode) · remove_pin_from_node(RemovePinFromNode) ·
        try_node_add_input_pin(TryNodeAddInputPin) ·
        set_node_property(SetNodeProperty) · add_cast_node(AddCastNode) ·
        add_class_cast_node(AddClassCastNode) · add_macro_node(AddMacroNode) ·
        add_switch_node(AddSwitchNode) · add_struct_node(AddStructNode) ·
        add_enum_literal_node(AddEnumLiteralNode) ·
        add_composite_node(AddCompositeNode) ·
        add_math_expression_node(AddMathExpressionNode) ·
        add_async_action_node(AddAsyncActionNode) ·
        add_create_delegate_node(AddCreateDelegateNode) ·
        add_actor_bound_event_node(AddActorBoundEventNode) ·
        add_input_axis_event_node(AddInputAxisEventNode) ·
        add_input_action_event_node(AddInputActionEventNode) ·
        add_component_bound_event_node(AddComponentBoundEventNode) ·
        remove_component(RemoveComponent) ·
        add_implemented_interface(AddImplementedInterface) ·
        add_event_dispatcher(AddEventDispatcher) ·

        ── 落盘（T-20260913-UE5BRIDGE-SAVE-ASSET · fail-loud）──
        save_asset(SaveAsset)

        ── 删除（T-20260913-UE5BRIDGE-DELETE-COMPILE-FIX · D-1 · fail-loud）──
        delete_asset(级联删除 + 删后回读 does_asset_exist + 重试 ≤3 → fail-loud)

        ── 遗留引用解绑（T-20260914-UE5BRIDGE-BUILD-HOLD-FIX · D-4 修正）──
        unpin_console_refs(asset_path — 解绑 PyConsoleGlobalDict 中指向目标资产的
            包装器 + collect_garbage；asset_path 缺省 → 可读错误，无全量清理语义)

        ── v3.0 新能力族（T-20260918-V3-MCP-FUSION · 9 条）──
        PIE 运行时验证：pie_start(mode=simulate|pie) · pie_stop · pie_state
        Actor 基础族（world=editor|pie）：find_actors · spawn_actor ·
            set_actor_transform · get_actor_properties ·
        verify_behavior(行为验证: 读运行时属性与期望比对 + 截图证据; 需 PIE 运行)
        日志查询：logs_read（游标增量 + category/pattern 过滤）
        批量执行：editor_batch（白名单命令序列单次请求；fail-loud 可中止）
        材质族（v3.0）：material_create · material_add_expression ·
            material_connect_expressions · material_connect_property ·
            material_set_expression_property · material_compile · material_read ·
            material_instance_create · material_instance_set_parameter
        UMG 控件族（v3.0，需 v3.0 版 DLL）：widget_create · widget_add_child ·
            widget_set_property · widget_read_tree

        ── v6.5 多 Agent / AnimBP（纯主机侧 + 只读）──
        session_lock_acquire(owner, assets, ttl, force) · session_lock_release ·
        session_lock_status —— 显式协作锁（任务锁之外的 opt-in 通道）；
        receipt_list(limit) —— 最近写回执（倒序，上限 200）；
        animbp_probe(asset_path) —— AnimBP 类/父类/骨骼/能力只读探测；
        animbp_read(asset_path, include_nodes, limit) —— 图/节点只读枚举，
        读不到 fail-loud；写入通道未启用（需真机验证）。

    命令可用性标注（T-20260820-UE5BRIDGE-API-GAP-FIX + T-20260822-ADDCOMPONENT-CPP）:
        - add_component: ✅ supported —— 底层优先调用 DLL
          add_component_to_blueprint（C++ SCS API，v0.9 T-20260822 新增，绕过
          UE5.1 Python 无 SimpleConstructionScript 暴露限制，见 KB
          UE5_Python_API_组件添加限制_SCS未暴露.md [已验证]）；DLL 不可用时
          降级 BlueprintEditorLibrary.add_component_to_blueprint（真实 UE5.1
          中无 SCS 绑定会失败，以 BridgeError 明确报错）。
        - add_function_node: ✅ 可用（A' 修复后）—— 底层调用 DLL
          add_function_call_node（UE 反射名），function_path 按最后一个 ':' 拆分。
    """
    global _request_count
    with _lock:
        _request_count += 1

    # ── Token 认证 ──
    if not _verify_token(request):
        _OP_LOG.warning(f"❌ /command: 未授权访问 (token 不匹配)")
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")

    # 检查白名单
    if req.command not in _COMMAND_WHITELIST:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown command: {req.command}. Available: {list(_COMMAND_WHITELIST.keys())}"
        )

    t0 = time.time()
    _OP_LOG.info(f"📋 /command: {req.command} params={str(req.params)[:200]}")

    try:
        _guard_call("ue5_execute_command",
                    {"command": req.command, "params": req.params,
                     "confirm": (req.params or {}).get("confirm")})
        _rctx = _receipt_begin(req.command, req.params)
        if req.command == "verify_scenario":
            # 场景含 wait/轮询：必须在 HTTP 线程跑，不能占 GT（会阻塞 PIE 帧）
            result = _dispatch_with_tx(
                req.command, _COMMAND_WHITELIST[req.command], **req.params)
        else:
            result = _gt_run_handler(
                _dispatch_with_tx, req.command,
                _COMMAND_WHITELIST[req.command], **req.params)
        # 截图闭环（T-20261024-UE5BRIDGE-SCREENSHOT）：GT 上下文里发起的 pending
        # 截图由本钩子在【HTTP 线程】收尾轮询（GT 上等文件 = 阻塞帧推进 = 死锁）。
        result = _capture_finish_pending(result)
        result = _receipt_finish(_rctx, result)
        # D-1（T-20260913-UE5BRIDGE-DELETE-COMPILE-FIX）：**opt-in** fail-loud 传播
        #   —— 白名单命令返回体带 `_fail_loud=True` 且 success=False 时，
        #   命令层 success 置 false（⛔ 不再返回假成功）。
        #   未标 `_fail_loud` 的既有命令行为**完全不变**（零回归面）。
        if (isinstance(result, dict) and result.get("_fail_loud")
                and not result.get("success")):
            return CommandResponse(
                success=False,
                command=req.command,
                result=result,
                error=str(result.get("error", "")),
                duration_ms=(time.time() - t0) * 1000,
            )
        return CommandResponse(
            success=True,
            command=req.command,
            result=result,
            duration_ms=(time.time() - t0) * 1000,
        )
    except Exception as e:
        return CommandResponse(
            success=False,
            command=req.command,
            error=f"{type(e).__name__}: {e}",
            duration_ms=(time.time() - t0) * 1000,
        )
#  POST /read — 蓝图读取（L1 类信息+变量+CDO / L2 节点+连线）
# ══════════════════════════════════════════════════════════

class ReadRequest(BaseModel):
    blueprint_path: str
    level: str = "L1"  # "L1" | "L2" | "full"
    include_cdo: bool = True
    include_connections: bool = True

@app.post("/read")
def read_blueprint(req: ReadRequest, request: Request = None):  # 🔧 B: async→sync def（T-20260806-UE5BRIDGE-CRASH-FIX 方案B·线程池隔离）
    """读取蓝图信息。

    L1（不依赖 C++ Bridge）：
      - 类信息（名称、类型、父类、接口）
      - 变量清单（名称/类型/默认值/是否暴露/Category）
      - CDO 默认值（可选）

    L2（依赖 C++ Bridge）：
      - 节点清单（类型/标题/位置/GUID/所在图）
      - 引脚详情（名称/方向/类型/默认值/是否连线）
      - 连线拓扑（From → To）

    Request:
        { "blueprint_path": "/Game/Blueprints/BP_Enemy", "level": "full" }
    """
    global _request_count
    with _lock:
        _request_count += 1

    # ── Token 认证（N-002 修复）──
    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")

    t0 = time.time()
    result = {
        "success": True,
        "blueprint_path": req.blueprint_path,
        "level": req.level,
    }

    bp_path = req.blueprint_path
    if not bp_path.startswith("/Game/"):
        bp_path = f"/Game/{bp_path}"

    try:
        # ═══ GameThread 安全读取（T-20260723-READFIX-v2）═══
        # unreal.EditorAssetLibrary.load_asset() 必须在 GameThread 执行。
        # Bridge Python wrapper 的 execute_python_on_game_thread(code) 只调度不返回结果，
        # 所以用临时文件桥接：GameThread 执行 → 写 JSON → uvicorn 线程读回。
        if BRIDGE_AVAILABLE and hasattr(bridge, 'execute_python_on_game_thread'):
            import tempfile
            result_file = os.path.join(os.environ.get("TEMP", "/tmp"), f"ue5_read_{uuid.uuid4().hex[:8]}.json")

            # T-20260723-READFIX BUG#3：repr(bp_path) 防字符串注入
            code = (
                f"import unreal, json, os\n"
                f"bp_path = {repr(bp_path)}\n"
                f"result = {{}}\n"
                f"try:\n"
                f"    bp = unreal.EditorAssetLibrary.load_asset(bp_path)\n"
                f"    if not bp:\n"
                f"        result = {{'error': '蓝图不存在: ' + bp_path}}\n"
                f"    else:\n"
                f"        # L1 info —— 补丁①(T-20260914-UE5BRIDGE-BUILD-HOLD-FIX)：\n"
                f"        #   统一走 ue5_bridge._read_l1_info_impl（与 /command read_blueprint 同源），\n"
                f"        #   ⛔ 不再自建一套 L1 —— 老写法 gc.get_super_class() 在 UE5.1 未暴露\n"
                f"        #   → 裸 except 静默吞错 → parent_class 恒空 + 缺 compile_status_source。\n"
                f"        #   ⚠️ 本脚本已在 GameThread 内 → 只能用 *_impl；调 _read_l1_info 会\n"
                f"        #   二次入队自等待 → 死锁超时。\n"
                f"        try:\n"
                f"            import ue5_bridge as _B\n"
                f"            result['l1'] = _B._read_l1_info_impl(bp)\n"
                f"        except Exception as __l1e:\n"
                f"            result['l1'] = {{}}\n"
                f"            result['l1_error'] = str(__l1e)\n"
                f"            print('[ue5_bridge GT] l1: ' + str(__l1e))\n"
                f"\n"
                f"        # Variables —— 补丁②(T-20260914-UE5BRIDGE-READPATH-FIX · D-5)：\n"
                f"        #   统一走 ue5_bridge._read_variables_impl（与 /command read_variables 同源），\n"
                f"        #   ⛔ 不再内联 bp.new_variables —— UE5.1 未暴露该属性\n"
                f"        #   → variables=[] 恒空 + variables_error（假阳性），与 C++ 通路结果不一致。\n"
                f"        #   ⚠️ 本脚本已在 GameThread 内 → 只能用 *_impl；调 _read_variables 会\n"
                f"        #   二次入队自等待 → 死锁超时（L4694 教训）。\n"
                f"        try:\n"
                f"            import ue5_bridge as _B\n"
                f"            result['variables'] = _B._read_variables_impl(bp)\n"
                f"        except Exception as __ve:\n"
                f"            result['variables'] = []\n"
                f"            result['variables_error'] = str(__ve)\n"
                f"            print('[ue5_bridge GT] variables: ' + str(__ve))\n"
            )
            if req.include_cdo:
                code += (
                    f"\n"
                    f"        # CDO\n"
                    f"        cdo_info = {{'accessible': False, 'properties': {{}}}}\n"
                    f"        try:\n"
                    f"            gc = bp.generated_class()\n"
                    f"            if gc:\n"
                    f"                cdo = gc.get_default_object()\n"
                    f"                if cdo:\n"
                    f"                    cdo_info['accessible'] = True\n"
                    f"                    for p in ['bCanEverTick', 'PrimaryActorTick', 'bReplicates', 'NetUpdateFrequency', 'InitialLifeSpan']:\n"
                    f"                        try: cdo_info['properties'][p] = str(getattr(cdo, p))\n"
                    f"                        except Exception as _cp_e: cdo_info.setdefault('_warnings',[]).append('CDO prop ' + str(p) + ' read failed: ' + str(_cp_e))\n"
                    f"        except Exception as _e: print('[ue5_bridge GT] cdo: ' + str(_e))\n"
                    f"        result['cdo'] = cdo_info\n"
                )
            # T-20260723-READFIX BUG#2：当 level 为 L2/full 时嵌入节点/连线读取
            if req.level in ("L2", "full"):
                code += (
                    f"\n"
                    f"        # L2: nodes + connections\n"
                    f"        l2 = {{'graphs': []}}\n"
                    f"        try:\n"
                    f"            graphs = [unreal.BlueprintPythonBridge.get_event_graph(bp)]\n"
                    f"        except:\n"
                    f"            try:\n"
                    f"                graphs = unreal.BlueprintPythonBridge.get_all_graphs(bp)\n"
                    f"            except:\n"
                    f"                graphs = []\n"
                    f"        for g in graphs:\n"
                    f"            if not g:\n"
                    f"                continue\n"
                    f"            gi = {{'graph_name': str(g.get_name()) if hasattr(g, 'get_name') else '?', 'node_count': 0, 'connection_count': 0, 'nodes': [], 'connections': []}}\n"
                    f"            try:\n"
                    f"                nodes = unreal.BlueprintPythonBridge.list_nodes(g)\n"
                    f"                if nodes:\n"
                    f"                    gi['node_count'] = len(nodes)\n"
                    f"                    for n in nodes:\n"
                    f"                        ne = {{'title': str(n.node_title) if hasattr(n, 'node_title') else '?', 'class': str(n.node_class) if hasattr(n, 'node_class') else '?', 'guid': ((n.node_guid.export_text() if hasattr(n.node_guid, 'export_text') else str(n.node_guid)) if hasattr(n, 'node_guid') else ''), 'pos': [int(n.pos_x) if hasattr(n, 'pos_x') else 0, int(n.pos_y) if hasattr(n, 'pos_y') else 0], 'pins': []}}\n"
                    f"                        try:\n"
                    f"                            pins = unreal.BlueprintPythonBridge.get_pin_infos(g, n)\n"
                    f"                            if pins:\n"
                    f"                                for p in pins:\n"
                    f"                                    ne['pins'].append({{'name': str(p.pin_name) if hasattr(p, 'pin_name') else '?', 'direction': str(p.direction) if hasattr(p, 'direction') else '?', 'category': str(p.pin_category) if hasattr(p, 'pin_category') else '', 'is_connected': bool(p.is_connected) if hasattr(p, 'is_connected') else False}})\n"
                    f"                        except:\n"
                    f"                            pass\n"
                    f"                        gi['nodes'].append(ne)\n"
                    f"            except Exception as __e2:\n"
                    f"                gi['node_error'] = str(__e2)\n"
                    f"            try:\n"
                    f"                conns = unreal.BlueprintPythonBridge.get_node_connections(g)\n"
                    f"                if conns:\n"
                    f"                    gi['connection_count'] = len(conns)\n"
                    f"                    for c in conns:\n"
                    f"                        gi['connections'].append({{'from_node': str(c.from_node_title) if hasattr(c, 'from_node_title') else '?', 'from_pin': str(c.from_pin_name) if hasattr(c, 'from_pin_name') else '?', 'to_node': str(c.to_node_title) if hasattr(c, 'to_node_title') else '?', 'to_pin': str(c.to_pin_name) if hasattr(c, 'to_pin_name') else '?'}})\n"
                    f"            except Exception as __e3:\n"
                    f"                gi['connection_error'] = str(__e3)\n"
                    f"            l2['graphs'].append(gi)\n"
                    f"        result['l2'] = l2\n"
                )
            code += (
                f"except Exception as __e:\n"
                f"    result = {{'error': str(__e)}}\n"
                f"\n"
                f"with open({repr(result_file)}, 'w', encoding='utf-8') as f:\n"
                f"    json.dump(result, f, ensure_ascii=False, default=str)\n"
            )

            # 🔧 E: 路径② trace_id 标记（T-20260806-UE5BRIDGE-CRASH-FIX 方案E）
            # 修法1（T-20260914-UE5BRIDGE-BUILD-HOLD-FIX · 阶段一 §1.6 第二处泄漏）：
            #   read 内联脚本原为模块级 `bp = load_asset(...)` → 同样写入持久
            #   PyConsoleGlobalDict 保活该资产。此处同法包裹。
            code = _wrap_gt_scope(code)
            _OP_LOG.info(f"[trace_id={getattr(request.state, 'trace_id', '?')}] PATH2 execute_python_on_game_thread(read_blueprint)")
            _gt_exec_python(code)
            # T-20260723-READFIX BUG#4：Future.Get() 已同步阻塞，无需 time.sleep

            if os.path.exists(result_file):
                with open(result_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                os.remove(result_file)

                if "error" in data and "l1" not in data:
                    raise BridgeError(data["error"], category="game_thread")
                result["l1"] = data.get("l1", {})
                # 补丁① fail-loud：GT 侧 L1 组装异常不吞（与 variables_error 同口径）
                if "l1_error" in data:
                    result["l1_error"] = data["l1_error"]
                result["variables"] = data.get("variables", [])
                # T-20260820-UE5BRIDGE-LEFTOVER-TAIL 路径A：透传 GT 侧变量读取错误，不吞
                if "variables_error" in data:
                    result["variables_error"] = data["variables_error"]
                if req.include_cdo:
                    result["cdo"] = data.get("cdo", {})
                # T-20260723-READFIX BUG#2：回传 L2 数据
                if req.level in ("L2", "full"):
                    result["l2"] = data.get("l2", {"error": "L2 数据未返回"})
            else:
                raise BridgeError("GameThread 执行未产出结果文件", category="game_thread")

        else:
            # 降级：无 C++ Bridge 时走 _run_on_game_thread_sync（可能崩，但不阻塞）
            bp = _resolve_blueprint(req.blueprint_path)
            result["l1"] = _read_l1_info(bp)
            result["variables"] = _read_variables(bp)
            if req.include_cdo:
                result["cdo"] = _read_cdo(bp)
            if req.level in ("L2", "full"):
                result["l2"] = {"available": False, "error": "C++ Bridge 插件未加载"}

        result["duration_ms"] = (time.time() - t0) * 1000

    except BridgeError as e:
        result["success"] = False
        result["error"] = str(e)
        result["category"] = e.category
        result["pitfall_id"] = e.pitfall_id
        result["suggestion"] = e.suggestion
        result["duration_ms"] = (time.time() - t0) * 1000
    except Exception as e:
        result["success"] = False
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()[-500:]
        result["duration_ms"] = (time.time() - t0) * 1000

    # ── T-20261024-UE5BRIDGE-DASHBOARD：reader 快照挂钩（fail-open）──
    # blueprint_reader 宿主 CLI 走本端点 —— L2/full 结果进仪表盘图纸快照缓存。
    try:
        _ui_dash.observe_read_result(req.blueprint_path, req.level, result)
    except Exception:
        pass  # 观测面永不阻塞读取主路径（例外声明见 add_trace_id 内注释）

    return result


def _wrap_gt_scope(code: str, entry: str = "_ue5_gt_main") -> str:
    """修法1（T-20260914-UE5BRIDGE-BUILD-HOLD-FIX）：把 GameThread 注入脚本的主体
    包进函数体，消灭「模块级 UObject 绑定」。

    根因：ExecPythonCommand / ExecuteFile 的 Public 作用域以**持久**
    PyConsoleGlobalDict 作 globals（PythonScriptPlugin.cpp L1416-1422：
    `PyFileGlobalDict = PyConsoleGlobalDict`）→ 脚本模块级 `bp` / `graph` 等
    UObject 包装器被 `FPyReferenceCollector(FGCObject)` 保活 → 资产 Package
    恒不可删（engine_reason=`no_on_disk_referencer_but_undeletable`）。
    函数返回后局部引用立即失去引用 → CPython refcount=0 → 包装器析构 →
    `PythonWrappedInstances.Remove` → GC 根消失。

    幂等：脚本已自带顶层 `def _ue5_gt_main():`（模板已预包裹）→ 原样返回。
    """
    if not code or ("def %s():" % entry) in code:
        return code
    body = "\n".join(("    " + ln) if ln.strip() else "" for ln in code.splitlines())
    return "def %s():\n%s\n%s()\n" % (entry, body, entry)


def _read_l1_info_impl(bp) -> Dict:
    """L1 信息**组装**（纯函数 · 必须在 GameThread 上调用）。

    🔴 单一权威源（T-20260914-UE5BRIDGE-BUILD-HOLD-FIX 补丁①）：
      `_read_l1_info(bp)` 与 HTTP `/read` 的 PATH2 注入脚本**共调本函数**，
      两条读路径的 L1 形状强制一致。改前 `/read` 自建一套老写法：
      `gc.get_super_class()` 在 UE5.1 未暴露 → 裸 `except:` 静默吞错 →
      `parent_class` 恒为 `""`、且缺 `compile_status_source`，
      与 `/command read_blueprint` 返回形状不一致。

    调用方线程职责（⚠️）：
      · **不在** GameThread → 调 `_read_l1_info(bp)`（自带调度壳）；
      · **已在** GameThread（如 PATH2 注入脚本）→ 直接调本函数；
        ⛔ 此时再调 `_read_l1_info` 会二次入队自等待 → 死锁/超时。

    红灯规则（KB: CDO_BEFORE_COMPILE P04）：
      不在编译前访问 CDO。
    """
    def _do_read():
        info = {
            "asset_name": "",
            "asset_path": "",
            "blueprint_type": "",
            "parent_class": "",
            "interfaces": [],
            "compile_status": "unknown",
        }
        try:
            info["asset_name"] = str(bp.get_name())
        except:
            pass
        try:
            info["asset_path"] = str(bp.get_path_name())
        except:
            pass
        # ── 3.1（T-20260914-UE5BRIDGE-BUILD-HOLD-FIX）：L1 读路径三处缺陷修复 ──
        #   a) UE5.1 Python 未暴露 UClass::get_super_class() → 不再调用该接口；
        #      改走 AssetRegistry 标签（blueprint_reader.py L467-483 实测写法）。
        #   b) 裸 `except: pass` 静默吞错 → 改 fail-loud（显式 *_error 字段）。
        #   c) compile_status 未传 compiled → 落到兜底 unknown；补
        #      compile_status_source 明确「引擎侧不可读」，不再静默冒充。
        try:
            _ad = unreal.EditorAssetLibrary.find_asset_data(str(info["asset_path"] or ""))
            try:
                _raw_type = str(_ad.get_tag_value("BlueprintType") or "")
            except Exception as _e:
                _raw_type = ""
                info["blueprint_type_error"] = str(_e)
            if _raw_type and _raw_type != "None":
                info["blueprint_type"] = _raw_type.replace("BPTYPE_", "")
            elif "blueprint_type_error" not in info:
                info["blueprint_type_error"] = "BlueprintType tag empty"
            _raw_parent = ""
            for _tag in ("ParentClass", "NativeParentClass"):
                try:
                    _val = _ad.get_tag_value(_tag)
                except Exception as _e:
                    info["parent_class_error"] = str(_e)
                    continue
                if _val and str(_val) != "None":
                    _raw_parent = str(_val)
                    break
            if _raw_parent:
                _tail = _raw_parent.split("'")[-2] if "'" in _raw_parent else _raw_parent
                info["parent_class"] = _tail.split(".")[-1] or _raw_parent
            elif "parent_class_error" not in info:
                info["parent_class_error"] = "ParentClass/NativeParentClass tag empty"
        except Exception as _e:
            info["parent_class_error"] = str(_e)
        # 接口清单：仍走既有 generated_class() 路径（非新增接口探测）；
        #   ⛔ 不再调用未暴露的 get_super_class()。
        try:
            _gen = bp.generated_class()
            if _gen:
                try:
                    info["interfaces"] = [str(i.get_name())
                                          for i in _gen.implemented_interfaces]
                except Exception as _e:
                    info["interfaces_error"] = str(_e)
        except Exception as _e:
            info["interfaces_error"] = str(_e)
        try:
            _cs = _bp_status_str(bp)
            info["compile_status"] = _cs
            if _cs in ("unknown", "", None):
                info["compile_status_source"] = "engine_status_unreadable_in_this_ue_version"
        except Exception as _e:
            info["compile_status_error"] = str(_e)
        return info

    return _do_read()


def _read_l1_info(bp) -> Dict:
    """L1：读取蓝图类基本信息（GameThread 调度壳）。

    组装逻辑见 `_read_l1_info_impl`（单一权威源）——本函数只负责「把组装
    排到 GameThread 上执行」。`/command read_blueprint`(L855) 与
    `/read` 降级路径 走本函数；HTTP `/read` 的 PATH2 注入脚本已在 GameThread
    内 → 直调 `_read_l1_info_impl`（补丁①，避免二次入队死锁）。
    """
    def _do():
        return _read_l1_info_impl(bp)

    return _run_on_game_thread_sync(_do, timeout=15.0)


def _read_variables_impl(bp) -> List[Dict]:
    """L1：读取蓝图变量清单**组装**（纯函数 · 必须在 GameThread 上调用）。

    🔴 单一权威源（T-20260914-UE5BRIDGE-READPATH-FIX 补丁② · D-5）：
      `_read_variables(bp)` 与 HTTP `/read` 的 PATH2 注入脚本**共调本函数**，
      两条读路径的变量形状强制一致。改前 `/read` 自建一套内联
      `bp.new_variables` 老写法 —— UE5.1 未暴露该属性 → variables=[] 恒空
      + variables_error（假阳性），与 `/command read_variables`（C++ 通路）不一致。
      · **不在** GameThread（uvicorn 线程）→ 调 `_read_variables(bp)` 调度壳；
      · **已在** GameThread（如 PATH2 注入脚本）→ **直接调本函数**；
        ⛔ 调 `_read_variables` 会二次入队自等待 → 死锁超时（L4694 教训）。

    红灯规则（KB: CDO_BEFORE_COMPILE P04）：
      只读 NewVariables，不读 CDO。

    T-20260823-UE5BRIDGE-CPP-APIS 子项2：优先走 C++ ReadVariables API——
      真实 UE Python 绑定无 new_variables 属性（get_editor_property protected
      拒绝）→ 旧路径假阳性/降级（T-20260820-UE5BRIDGE-LEFTOVER-TAIL 遗留②）；
      C++ 反射读取 UBlueprint::NewVariables 为权威变量清单（含类型）。
      降级路径（fake/旧 DLL 无 read_variables 时）保留原逻辑。

    线程安全性：
      bp.new_variables 的迭代和每个 var.* 属性访问（var_name / var_type
      / default_value / category / instance_editable）都需要 GameThread。
    """
    def _do_read():
        # C++ API 优先：bridge.read_variables(bp) → TArray<FBPVariableInfo>
        # 真实 UE 反射返回结构体数组（item.variable_name），fake 返回 dict
        # （item["variable_name"]）——两者兼容处理。
        if BRIDGE_AVAILABLE and hasattr(bridge, 'read_variables'):
            raw = _safe_call(bridge.read_variables, bp,
                             error_cat="variables_read")
            variables = []
            for item in (raw or []):
                if isinstance(item, dict):
                    v_name = item.get("variable_name", "")
                    v_type = item.get("variable_type", "")
                    v_default = item.get("default_value", "")
                    v_category = item.get("category", "")
                    v_editable = bool(item.get("is_editable", False))
                else:
                    v_name = getattr(item, "variable_name", "")
                    v_type = getattr(item, "variable_type", "")
                    v_default = getattr(item, "default_value", "")
                    v_category = getattr(item, "category", "")
                    v_editable = bool(getattr(item, "is_editable", False))
                variables.append({
                    "name": str(v_name),
                    "type": str(v_type),
                    "default_value": str(v_default),
                    "is_exposed": False,
                    "category": str(v_category),
                    "is_editable": v_editable,
                })
            return variables

        # 降级路径：Python new_variables（fake / 旧 DLL）
        variables = []
        try:
            for var in bp.new_variables:
                var_info = {
                    "name": "",
                    "type": "",
                    "default_value": "",
                    "is_exposed": False,
                    "category": "",
                    "is_editable": False,
                }
                try:
                    var_info["name"] = str(var.var_name)
                except:
                    pass
                try:
                    var_info["type"] = str(var.var_type.type_name) if hasattr(var.var_type, "type_name") else str(var.var_type)
                except:
                    pass
                try:
                    var_info["default_value"] = str(var.default_value)
                except:
                    pass
                try:
                    var_info["is_exposed"] = bool(getattr(var, "has_default_value", False))
                except:
                    pass
                try:
                    var_info["category"] = str(var.category) if hasattr(var, "category") else ""
                except:
                    pass
                try:
                    var_info["is_editable"] = bool(getattr(var, "instance_editable", False))
                except:
                    pass
                variables.append(var_info)
        except Exception as e:
            # T-20260820-UE5BRIDGE-LEFTOVER-TAIL 路径A：不再吞异常（假阳性），
            # 如实抛 BridgeError → 命令层 success=False + error 明确字段
            raise BridgeError(
                f"读取变量失败: {e}",
                category="variables_read",
                suggestion="当前 UE Python 绑定未暴露 new_variables 属性，无法程序化读取变量清单；可用 add/remove 变量操作反证变量存在"
            )
        return variables

    return _do_read()


def _read_variables(bp) -> List[Dict]:
    """L1：读取蓝图变量清单（GameThread 调度壳）。

    组装逻辑见 `_read_variables_impl`（单一权威源）——本函数只负责「把组装
    排到 GameThread 上执行」。`/command read_variables`(L868) 与
    `/read` 降级路径 走本函数；HTTP `/read` 的 PATH2 注入脚本已在 GameThread
    内 → 直调 `_read_variables_impl`（补丁②，避免二次入队死锁）。
    """
    return _run_on_game_thread_sync(_read_variables_impl, bp, timeout=15.0)


def _read_cdo(bp) -> Dict:
    """L1：读取 CDO 默认值。

    红灯规则（KB: CDO_BEFORE_COMPILE P04, P03）：
      - 确保 GeneratedClass 已存在才访问 CDO
      - 编译前不访问 CDO
      - 读取后不修改（修改必须在编译前做）

    线程安全性：
      bp.generated_class() / gen_class.get_default_object() /
      getattr(cdo, ...) 全部需要 GameThread。
    """
    def _do_read():
        cdo_info = {"accessible": False, "properties": {}, "warnings": []}
        try:
            gen_class = bp.generated_class()
            if not gen_class:
                cdo_info["warnings"].append("GeneratedClass 为空，跳过 CDO 读取")
                return cdo_info
            cdo = gen_class.get_default_object()
            if not cdo:
                cdo_info["warnings"].append("CDO 不可访问")
                return cdo_info
            cdo_info["accessible"] = True

            key_props = [
                "bCanEverTick", "PrimaryActorTick", "bReplicates",
                "NetUpdateFrequency", "InitialLifeSpan",
            ]
            for prop_name in key_props:
                try:
                    val = getattr(cdo, prop_name, None)
                    if val is not None:
                        cdo_info["properties"][prop_name] = str(val)
                except Exception as _cp_e:
                    cdo_info.setdefault("_warnings", []).append(
                        f"CDO 属性 {prop_name} 读取失败: {_cp_e}"
                    )
        except Exception as e:
            cdo_info["error"] = str(e)
        return cdo_info

    return _run_on_game_thread_sync(_do_read, timeout=15.0)


def _read_l2_nodes(bp, include_connections: bool = True) -> Dict:
    """L2：通过 C++ Bridge 读取节点、引脚、连线拓扑。

    依赖：BlueprintPythonBridge 插件（21 API）

    红灯规则（KB: P01 ConnectPins 假阳性 / P02 静默失败）：
      - 读取时验证每个节点/引脚是否存在
      - 连线数据通过 get_node_connections 获取（已验证的拓扑）

    线程安全性：
      bridge.* 调用内部通过 AsyncTask 调度到 GameThread，但 graph.get_name()、
      hasattr(n, "node_title") 等 unreal 对象属性访问仍需 GameThread。
      整体包裹在 _run_on_game_thread_sync 中。
    """
    def _do_read():
        """🔧 T-20260723-NODEDEADLOCK：此函数在 GameThread 内执行（tick worker），
        不能用 bridge.* 包装函数（它们内部 AsyncTask 会嵌套死锁）。
        直接用 unreal.BlueprintPythonBridge.* 静态方法。"""
        l2 = {"available": True, "graphs": []}
        BPB = unreal.BlueprintPythonBridge

        try:
            graphs = BPB.get_all_graphs(bp)
        except:
            try:
                graphs = [BPB.get_event_graph(bp)]
            except:
                l2["error"] = "无法获取蓝图图表"
                return l2

        for graph in graphs:
            if not graph:
                continue
            graph_info = {
                "graph_name": str(graph.get_name()) if hasattr(graph, "get_name") else "unknown",
                "node_count": 0,
                "connection_count": 0,
                "nodes": [],
                "connections": [],
            }

            # ── 读取节点 ──
            try:
                nodes = BPB.list_nodes(graph)
                graph_info["node_count"] = len(nodes) if nodes else 0
                if nodes:
                    for n in nodes:
                        node_entry = {
                            "title": str(n.node_title) if hasattr(n, "node_title") else "?",
                            "class": str(n.node_class) if hasattr(n, "node_class") else "?",
                            # T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE C-①：`str(BPNodeInfo.node_guid)`
                            # 在真实 UE 上返回 `<Struct 'Guid' (0x内存地址) {}>`（不可用于 get_node_by_guid），
                            # 改用规范归一化助手 → 32 位 hex，使 read_nodes.guid 与 get_node_by_guid 可直通。
                            "guid": _pywrap_node_guid(n),
                            "pos": [int(n.pos_x) if hasattr(n, "pos_x") else 0,
                                    int(n.pos_y) if hasattr(n, "pos_y") else 0],
                            "pins": [],
                        }
                        try:
                            pins = BPB.get_pin_infos(graph, n)
                            if pins:
                                for p in pins:
                                    node_entry["pins"].append({
                                        "name": str(p.pin_name) if hasattr(p, "pin_name") else "?",
                                        "direction": str(p.direction) if hasattr(p, "direction") else "?",
                                        "category": str(p.pin_category) if hasattr(p, "pin_category") else "",
                                        "sub_category": str(p.pin_sub_category) if hasattr(p, "pin_sub_category") else "",
                                        "is_connected": bool(p.is_connected) if hasattr(p, "is_connected") else False,
                                        "default_value": str(p.default_value) if hasattr(p, "default_value") else "",
                                    })
                        except:
                            pass
                        graph_info["nodes"].append(node_entry)
            except Exception as e:
                graph_info["node_error"] = str(e)

            # ── 读取连线 ──
            if include_connections:
                try:
                    conns = BPB.get_node_connections(graph)
                    graph_info["connection_count"] = len(conns) if conns else 0
                    # v2.20：连线补 from_guid/to_guid（由同图 nodes 的 title→guid 映射反查）
                    #   —— 客户端（blueprint_editor._get_pin_links 等）拿 GUID 定位节点时
                    #   按 title 匹配永远落空 → 假「本就无连接」。有 GUID 后可精确匹配。
                    _title2guid = {}
                    for _n in graph_info["nodes"]:
                        _t = _n.get("title")
                        _g = _n.get("guid")
                        if _t and _g and _t not in _title2guid:
                            _title2guid[_t] = _g
                    if conns:
                        for c in conns:
                            _from_t = str(c.from_node_title) if hasattr(c, "from_node_title") else "?"
                            _to_t = str(c.to_node_title) if hasattr(c, "to_node_title") else "?"
                            graph_info["connections"].append({
                                "from_node": _from_t,
                                "from_pin": str(c.from_pin_name) if hasattr(c, "from_pin_name") else "?",
                                "to_node": _to_t,
                                "to_pin": str(c.to_pin_name) if hasattr(c, "to_pin_name") else "?",
                                "from_guid": _title2guid.get(_from_t, ""),
                                "to_guid": _title2guid.get(_to_t, ""),
                            })
                except Exception as e:
                    graph_info["connection_error"] = str(e)

            l2["graphs"].append(graph_info)

        return l2

    return _run_on_game_thread_sync(_do_read, timeout=30.0)  # L2 读取节点较多，给 30s


# ══════════════════════════════════════════════════════════
#  D-5（T-20260917-D5D6-CONTRACT-FIX）· `events[]` 字段名契约归一化
# ══════════════════════════════════════════════════════════
#  契约：**`event_name` 为主字段**，**`name` 为兼容别名**（历史文档 / runner
#  docstring / 既有脚本均写 `name`，不得改坏）。
#
#  缺陷史（maintainer 2026-09-17 活体 UE 实证）：`/build` 曾把 `req.events` **原样透传**
#  给 `game_thread_build.py.tmpl`，而模板只认 `event_name` → 照文档写 `name` 时：
#      steps 出现 `add_event:`（**事件名为空**）
#      + `error: "add_event_node returned None"` + `compile_status=BS_Error`
#  → 报错完全指不到根因字段（第三方开箱即用路径必踩）。
#
#  归一化规则（判据 R5-1 ~ R5-4）：
#    R5-1  仅 `name`              → 采用之（向后兼容既有文档/脚本）
#    R5-2  仅 `event_name`        → 采用之（现行生效字段，不回退）
#    R5-3  两者都给且**值不同**   → 取 `event_name` + 回传 warning（绝不静默择一）
#    R5-4  两者**都缺/为空**      → BridgeError **fail-loud**：点名 `events[i]`
#                                   + 回传实际收到的 keys（不再落到
#                                   `add_event_node returned None` 这种无根因报错）
def _normalize_build_events(events) -> tuple:
    """归一化 `/build` 的 `events[]` → `(normalized_events, warnings)`。

    · 每个 event 归一化后**同时**携带 `event_name`（主）与 `name`（别名镜像），
      `pos` 缺省补 `[0, 0]` → 下游（模板 / 任何读取方）取任一键都一致。
    · 非法项（非 dict / 无事件名）→ **raise BridgeError**，由 `/build` 的
      `except BridgeError` 转成结构化失败返回（`category="param_type"`）。
    """
    normalized: List[Dict] = []
    warnings: List[str] = []
    for _i, _raw in enumerate(events or []):
        if not isinstance(_raw, dict):
            raise BridgeError(
                "events[%d] 类型错误：应为对象（dict），实收 %s"
                % (_i, type(_raw).__name__),
                category="param_type",
                suggestion='events 每项形如 {"event_name": "ReceiveBeginPlay", '
                           '"pos": [0, 0]}',
            )
        ev = dict(_raw)
        _primary = str(ev.get("event_name") or "").strip()
        _alias = str(ev.get("name") or "").strip()
        if _primary and _alias and _primary != _alias:
            # R5-3：绝不静默择一 —— 取主字段 + 显式告警
            warnings.append(
                "【D-5】events[%d] 同时提供 name=%r 与 event_name=%r 且值不同 → "
                "取 event_name=%r（name 被忽略）" % (_i, _alias, _primary, _primary))
        elif not _primary and _alias:
            # R5-1：别名可用（仅提示迁移，不阻断）
            warnings.append(
                "【D-5】events[%d] 使用兼容别名 name=%r；推荐主字段 event_name"
                "（两者均受支持）" % (_i, _alias))
        _canonical = _primary or _alias
        if not _canonical:
            # R5-4：fail-loud + 点名索引 + 回传实收 keys
            raise BridgeError(
                "events[%d] 缺少事件名（请提供 event_name）：实际收到的 keys=%s"
                % (_i, sorted(ev.keys())),
                category="param_type",
                suggestion='写 {"event_name": "ReceiveBeginPlay", "pos": [0, 0]}；'
                           'name 为兼容别名，可继续使用',
            )
        ev["event_name"] = _canonical
        ev["name"] = _canonical          # 别名镜像：任何读方取值一致
        if not ev.get("pos"):
            ev["pos"] = [0, 0]
        normalized.append(ev)
    return normalized, warnings


# ══════════════════════════════════════════════════════════
#  D-8（T-20260917-D8-FUNCTIONS-CONTRACT）：functions[] 字段名归一化
# ══════════════════════════════════════════════════════════
#
#  缺陷（与 D-5 `events[]` **完全同型**）：`/build` 曾把 `req.functions`
#  **原样透传**给 `game_thread_build.py.tmpl`，而模板读的是 `function_name` /
#  `function_path`（tmpl L307-309）；`ue5_runner.py` L410 的 docstring 却写
#  `{"name": ..., "target_class": ...}` → 第三方照文档写：
#      fn_name 空 + fn_path 空 → 走 tmpl `else` 分支
#      → `add_function_node(graph, '')` → 返回 None
#      → step `add_function:`（**函数名为空**）
#        + error「add_function_node returned None…常见原因：① 函数名不存在
#          ② 该类不含此函数 ③ 非 UFUNCTION ④ 类路径拼写/大小写错误」
#  → **四条提示全指向错方向**（真实原因 = 键名写错），第三方无从排查。
#
#  归一化规则（判据 K2 ~ K5）：
#    K3  仅 `function_name`（主字段）        → 采用之（现行字段不回退）
#    K2  仅 `name`（兼容别名）+ target_class → 采用之 + 推导 function_path
#    K4  两者都给且**值不同**                → 取 `function_name` + 回传 warning
#    K5  两者皆缺/为空且无 `function_path`   → BridgeError **fail-loud**：点名
#                                              `functions[i]` + 回传实收 keys
#                                              （不再落「空函数名 + returned None」）
def _normalize_build_functions(functions) -> tuple:
    """归一化 `/build` 的 `functions[]` → `(normalized_functions, warnings)`。

    · `function_name`（主）/ `name`（兼容别名）→ 归一化后**同时**携带两者
      （别名镜像）→ 下游（模板 / 任何读取方）取任一键都一致。
    · `function_path`（主，形如 `/Script/模块.类名:函数名`）；缺省时若
      `target_class` + 函数名齐备 → 由 `<target_class>:<函数名>` 推导等价路径，
      并在返回体 `warnings` 中**标注来源=target_class 推导**（同时写
      `function_path_source` 供程序化审计）。
    · 反向亦然：只给 `function_path` → 由路径末段推导函数名（同样告警标注）。
    · 非法项（非 dict / 函数名与路径皆缺）→ **raise BridgeError**，由 `/build` /
      `/build-batch` 的 `except BridgeError` 转成结构化失败（`category="param_type"`）。
    """
    normalized: List[Dict] = []
    warnings: List[str] = []
    for _i, _raw in enumerate(functions or []):
        if not isinstance(_raw, dict):
            raise BridgeError(
                "functions[%d] 类型错误：应为对象（dict），实收 %s"
                % (_i, type(_raw).__name__),
                category="param_type",
                suggestion='functions 每项形如 {"function_name": "PrintString", '
                           '"function_path": "/Script/Engine.KismetSystemLibrary:PrintString"}',
            )
        fn = dict(_raw)
        _primary = str(fn.get("function_name") or "").strip()
        _alias = str(fn.get("name") or "").strip()
        if _primary and _alias and _primary != _alias:
            # K4：绝不静默择一 —— 取主字段 + 显式告警
            warnings.append(
                "【D-8】functions[%d] 同时提供 name=%r 与 function_name=%r 且值不同 → "
                "取 function_name=%r（name 被忽略）" % (_i, _alias, _primary, _primary))
        elif not _primary and _alias:
            # K2：别名可用（仅提示迁移，不阻断）
            warnings.append(
                "【D-8】functions[%d] 使用兼容别名 name=%r；推荐主字段 function_name"
                "（两者均受支持）" % (_i, _alias))
        _canonical = _primary or _alias

        # ── 路径：主字段 → target_class 推导 ──
        _path = str(fn.get("function_path") or "").strip()
        _tc = str(fn.get("target_class") or "").strip()
        if _path:
            fn["function_path_source"] = "explicit"
        elif _tc and _canonical:
            _path = "%s:%s" % (_tc, _canonical)
            fn["function_path_source"] = "derived_from_target_class"
            warnings.append(
                "【D-8】functions[%d] 未提供 function_path → 由 target_class=%r + "
                "函数名=%r 推导出等价路径 %r（来源：target_class 推导）"
                % (_i, _tc, _canonical, _path))
        elif not _canonical:
            # K5：路径与函数名皆缺 → fail-loud + 点名索引 + 回传实收 keys
            raise BridgeError(
                "functions[%d] 缺少函数名（请提供 function_name）：实际收到的 keys=%s"
                % (_i, sorted(fn.keys())),
                category="param_type",
                suggestion='写 {"function_name": "PrintString", "function_path": '
                           '"/Script/Engine.KismetSystemLibrary:PrintString"}；'
                           'name（兼容别名）/ target_class（路径推导）均可继续使用',
            )

        # ── 反向：只给 function_path → 由路径末段推导函数名 ──
        if not _canonical and _path:
            _canonical = _path.rpartition(":")[2].strip()
            if _canonical:
                fn["function_name_source"] = "derived_from_function_path"
                warnings.append(
                    "【D-8】functions[%d] 未提供函数名 → 由 function_path=%r 推导出 "
                    "函数名 %r（来源：function_path 推导）"
                    % (_i, _path, _canonical))
        if not _canonical:
            raise BridgeError(
                "functions[%d] 缺少函数名（请提供 function_name）：实际收到的 keys=%s"
                % (_i, sorted(fn.keys())),
                category="param_type",
                suggestion='写 {"function_name": "PrintString", "function_path": '
                           '"/Script/Engine.KismetSystemLibrary:PrintString"}',
            )

        fn["function_name"] = _canonical
        fn["name"] = _canonical          # 别名镜像：任何读方取值一致
        if _path:
            fn["function_path"] = _path
        if not fn.get("pos"):
            fn["pos"] = [0, 0]
        normalized.append(fn)
    return normalized, warnings


# ══════════════════════════════════════════════════════════
#  POST /build — 创建蓝图资产/添加变量/组件/编译
# ══════════════════════════════════════════════════════════

class BuildRequest(BaseModel):
    blueprint_name: str
    package_path: str = "/Game/Blueprints/"
    parent_class: str = "Actor"
    blueprint_type: str = "Actor"
    variables: List[Dict] = []         # [{name, type, default_value, category, expose}]
    components: List[Dict] = []        # [{name, component_class, properties: {}}]
    interfaces: List[str] = []          # ["IInteractable", "IDamageable"]
    events: List[Dict] = []             # [{event_name, name(兼容别名), pos:[x,y], b_override}]
    functions: List[Dict] = []          # [{function_name, name(兼容别名), function_path,
                                        #   target_class(→推导 function_path), defaults:{pin:value}, pos:[x,y]}]
    connections: List[Dict] = []        # [{src_node, src_pin, dst_node, dst_pin}]
    compile_after: bool = True          # 创建后自动编译

@app.post("/build")
def build_blueprint(req: BuildRequest, request: Request = None):  # 🔧 B: async→sync def（T-20260806-UE5BRIDGE-CRASH-FIX 方案B·线程池隔离）
    """创建蓝图资产（骨架）。

    Request:
        {
            "blueprint_name": "BP_PickupItem",
            "parent_class": "Actor",
            "variables": [
                {"name": "PickupRadius", "type": "float", "default_value": "200.0", "category": "Pickup", "expose": true}
            ],
            "components": [
                {"name": "CollisionSphere", "component_class": "SphereComponent"}
            ],
            "events": [
                {"event_name": "ReceiveActorEndOverlap", "pos": [768, 0]}
            ],
            "compile_after": true
        }

    events[] 字段名契约（D-5 · T-20260917-D5D6-CONTRACT-FIX）：
      · `event_name` = **主字段**（如 `ReceiveBeginPlay` / `ReceiveActorEndOverlap`）
      · `name`       = **兼容别名**（历史文档/脚本写法，等价可用）
      · 两者都给且值不同 → 取 `event_name` 并在返回体 `warnings` 中显式告警
      · 两者都缺/为空   → **400 语义的结构化失败**，错误点名 `events[i]` + 实收 keys

    functions[] 字段名契约（D-8 · T-20260917-D8-FUNCTIONS-CONTRACT）：
      · `function_name` = **主字段**（如 `PrintString`）；`name` = **兼容别名**
      · `function_path` = **主字段**，格式 `/Script/模块.类名:函数名`
        （如 `/Script/Engine.KismetSystemLibrary:PrintString`）；
        缺省时若 `target_class` + 函数名齐备 → 由 `<target_class>:<函数名>` 推导
      · 主字段与别名都给且值不同 → 取 `function_name` 并在返回体 `warnings` 告警
      · 两者都缺/为空且无 `function_path` → **结构化失败**，错误点名 `functions[i]`
        + 实收 keys（不再落「空函数名 + add_function_node returned None」）

    红灯规则：
      - P07: 用 FKismetEditorUtilities 而非 AssetTools（CDO 正确初始化）
      - P04: 编译前设变量（编译后设的值不持久化）
      - P05: 添加事件节点需设 bOverrideFunction=True
      - P08: 每次操作重新加载蓝图（不缓存引用）
    """
    global _request_count
    with _lock:
        _request_count += 1

    # ── Token 认证（N-002 修复）──
    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")

    t0 = time.time()
    result = {
        "success": True,
        "blueprint_name": req.blueprint_name,
        "steps": [],
        "warnings": [],                 # D-5：events 字段名归一化告警（含 name/event_name 冲突）
    }

    bp = None
    try:
        # ── D-5（T-20260917-D5D6-CONTRACT-FIX）：events 字段名先归一化再进 GT ──
        #   置于任何副作用（建资产 / 改变量 / 加组件）**之前** → spec 不合法时
        #   fail-loud 且不留半成品资产（BridgeError 走下方 except → 结构化返回）。
        req.events, _ev_warnings = _normalize_build_events(req.events)
        result["warnings"].extend(_ev_warnings)

        # ── D-8（T-20260917-D8-FUNCTIONS-CONTRACT）：functions 字段名先归一化再进 GT ──
        #   与 D-5 **同型同位置**：契约与实现不一致（模板只认 function_name /
        #   function_path，文档写 name / target_class）→ 第三方照文档写必踩
        #   「空函数名 + add_function_node returned None」（4 条提示全指错方向）。
        #   BridgeError 走下方 except → 结构化返回，且**早于任何副作用**。
        req.functions, _fn_warnings = _normalize_build_functions(req.functions)
        result["warnings"].extend(_fn_warnings)

        # ── Step 1: 创建蓝图资产 ──
        full_path = f"{req.package_path}{req.blueprint_name}"
        if not _safe_game_pkg(full_path):
            raise BridgeError("build 只允许规范 /Game/ 路径（收到 %r）"
                              % full_path, category="param_type")
        obj_path = f"{full_path}.{req.blueprint_name}"

        # 🔧 T-20260724-GTFIX: does_asset_exist 必须在 GameThread 上调用
        #    在 uvicorn 线程调用返回 False（即使 BP 存在）→ 改用 _run_on_game_thread_sync
        existing = _run_on_game_thread_sync(unreal.EditorAssetLibrary.does_asset_exist, full_path)
        if not existing:
            existing = _run_on_game_thread_sync(unreal.EditorAssetLibrary.does_asset_exist, obj_path)
        if existing:
            raise BridgeError(
                f"蓝图已存在: {full_path}",
                category="asset_create",
                suggestion="请使用不同的蓝图名称或先删除已有蓝图"
            )

        # ═══════════════════════════════════════
        # Route: POST /build  — applies below
        # ═══════════════════════════════════════

        if BRIDGE_AVAILABLE:  # T-20260723-BYPASS2-DONE: DLL rebuilt with ExecutePythonOnGameThread
            bp = _safe_call(
                bridge.create_actor_blueprint,
                req.package_path, req.blueprint_name,
                error_cat="asset_create",
                pitfall_id="P07"
            )
        else:
            # 降级：用 unreal API 创建
            factory = unreal.BlueprintFactory()
            factory.set_editor_property("parent_class",
                unreal.load_class(None, f"/Script/Engine.{req.parent_class}"))
            bp = _safe_call(
                unreal.AssetToolsHelpers.get_asset_tools().create_asset,
                req.blueprint_name, req.package_path.rstrip("/"),
                None, factory,
                error_cat="asset_create"
            )

        result["steps"].append({
            "step": "create_asset",
            "status": "ok",
            "asset_path": str(bp.get_path_name()) if bp else "unknown"
        })

        # ── Step 2: 添加变量（T-20260909-USER-DRILL：写后回读验证）──
        vars_ok = True
        for var_def in req.variables:
            var_name = var_def.get("name", "")
            var_type = var_def.get("type", "bool")
            if not _add_variable_verified(bp, var_name, var_type, var_def,
                                          result["steps"]):
                vars_ok = False
        if not vars_ok:
            result["success"] = False
            result["error"] = ("部分变量未写入（见 steps 中 status=failed 项）；"
                               "蓝图资产已创建，可修正类型后重新添加")

        # ── Step 3: 添加组件 ──
        # T-20260909-SKILL-DEFECT-FIX：改走 _add_component_preferred（C++ SCS
        # API 优先），与 /command add_component 语义一致；legacy 路径依赖
        # UE5.1 未暴露的 SimpleConstructionScript → 创建模式带组件必然失败。
        for comp_def in req.components:
            comp_name = comp_def.get("name", "")
            comp_class = comp_def.get("component_class", "SceneComponent")
            _add_component_preferred(
                bp, comp_name, comp_class, comp_def.get("properties", {}))
            result["steps"].append({
                "step": f"add_component:{comp_name}",
                "status": "ok"
            })

        # ── Step 4: 设置接口 ──
        for iface in req.interfaces:
            _safe_call(
                _add_interface_unreal, bp, iface,
                error_cat="interface_add"
            )
            result["steps"].append({
                "step": f"add_interface:{iface}",
                "status": "ok"
            })

        # ── Steps 5-8: 节点/连线/编译（GameThread 原子执行）──
        # T-20260724-GTFIX: 所有图形操作通过 execute_python_on_game_thread 在 GameThread
        # 原子执行，消除 uvicorn 线程与 GameThread 之间的对象引用失效问题。
        # ── 修法2（T-20260914-UE5BRIDGE-BUILD-HOLD-FIX · 纵深防御）──
        #   仅「真有图形操作」才进 GT：GT 注入会经 ExecPythonCommand 写入**持久**
        #   PyConsoleGlobalDict。修法1 已在源头断链，此处再加一层——纯编译不再注入。
        _need_gt_ops = bool(req.events or req.functions or req.connections)
        if _need_gt_ops:
            spec_file = os.path.join(
                os.environ.get("TEMP", "/tmp"),
                f"ue5_build_spec_{uuid.uuid4().hex[:8]}.json"
            )
            result_file = os.path.join(
                os.environ.get("TEMP", "/tmp"),
                f"ue5_build_result_{uuid.uuid4().hex[:8]}.json"
            )

            # 将 spec 写入临时文件（GameThread 脚本读取）
            gt_spec = {
                "blueprint_path": full_path,
                "events": req.events,
                "functions": req.functions,
                "connections": req.connections,
                "compile_after": req.compile_after,
            }
            with open(spec_file, "w", encoding="utf-8") as f:
                json.dump(gt_spec, f, ensure_ascii=False)

            # 生成 GameThread 执行脚本（从模板文件加载，N01 修复）
            with open(os.path.join(os.path.dirname(__file__), "game_thread_build.py.tmpl"), "r", encoding="utf-8") as _f:
                gt_code = _wrap_gt_scope(_f.read().replace("{SPEC_FILE}", repr(spec_file)).replace("{RESULT_FILE}", repr(result_file)))

            if BRIDGE_AVAILABLE and hasattr(bridge, 'execute_python_on_game_thread'):
                # 🔧 E: 路径② trace_id 标记（T-20260806-UE5BRIDGE-CRASH-FIX 方案E）
                _OP_LOG.info(f"[trace_id={getattr(request.state, 'trace_id', '?')}] PATH2 execute_python_on_game_thread(build_blueprint)")
                _gt_exec_python(gt_code)

                # 🔴 T-20260724-D34FIX: 轮询等待 GameThread 完成，避免 TOCTOU 竞态
                _timeout = 30.0
                _interval = 0.1
                _waited = 0.0
                while not os.path.exists(result_file) and _waited < _timeout:
                    time.sleep(_interval)
                    _waited += _interval

                if os.path.exists(result_file):
                    with open(result_file, 'r', encoding='utf-8') as f:
                        gt_result = json.load(f)
                    os.remove(result_file)
                    for step in gt_result.get("steps", []):
                        result["steps"].append(step)
                    # D-5：模板侧告警（name/event_name 冲突等）并入返回体
                    result.setdefault("warnings", []).extend(
                        gt_result.get("warnings") or [])
                    if not gt_result.get("success"):
                        result["success"] = False
                        result["error"] = gt_result.get("error", "GameThread build failed")
                else:
                    # 🔧 B4 v0.5 修复：超时时返回部分成功状态，告知调用方已完成步骤
                    result["success"] = False
                    result["partial_success"] = True
                    result["blueprint_path"] = full_path
                    result["steps_completed"] = [s["step"] for s in result["steps"]]
                    result["steps_remaining"] = ["events", "functions", "connections", "compile"]
                    result["error"] = (
                        f"GameThread 执行超时（{_timeout}s）。"
                        f"蓝图资产已创建于 {full_path}，变量/组件/接口已添加，"
                        f"但节点/连线/编译未完成。请手动检查 Content Browser 并清理或继续编辑。"
                    )
            else:
                result["success"] = False
                result["error"] = "Bridge DLL 不可用或缺少 execute_python_on_game_thread；无法执行图形操作"

            # 清理 spec 文件
            try:
                os.remove(spec_file)
            except Exception:
                pass

        elif req.compile_after:
            # 修法2：纯编译（无 events/functions/connections）→ 走**进程内**编译，
            #   零 GT 注入 → 零 PyConsoleGlobalDict 写入。与 _cmd_build_blueprint
            #   同路径（_cmd_compile_blueprint → _resolve_blueprint + bridge 编译）。
            try:
                _cinfo = _cmd_compile_blueprint(full_path)
                _cok = bool(_cinfo.get("success", True))
                _step = {"step": "compile",
                         "status": "ok" if _cok else "error",
                         "path": "in_process(修法2)"}
                if not _cok:
                    _step["error"] = str(_cinfo.get("error", "compile failed"))
                    result["success"] = False
                    result["error"] = str(_cinfo.get("error", "compile failed"))
                result["steps"].append(_step)
            except Exception as _ce:
                result["success"] = False
                result["steps"].append({"step": "compile", "status": "error",
                                        "path": "in_process(修法2)", "error": str(_ce)})

        # ── D-2 强制回读：/build 完成后把实际 compile_status 写进返回体 ──
        try:
            result["compile_status"] = (
                _bp_status_str_gt(bp, compiled=bool(result.get("success")))
                if bp is not None else "unknown")
        except Exception:
            result["compile_status"] = "unknown"

        result["duration_ms"] = (time.time() - t0) * 1000

    except BridgeError as e:
        result["success"] = False
        result["error"] = str(e)
        result["category"] = e.category
        result["pitfall_id"] = e.pitfall_id
        result["suggestion"] = e.suggestion
        result["duration_ms"] = (time.time() - t0) * 1000
    except Exception as e:
        result["success"] = False
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()[-500:]
        result["duration_ms"] = (time.time() - t0) * 1000

    return result


def _add_variable_unreal(bp, var_name: str, var_type: str, var_def: Dict):
    """降级方案：用 unreal Python API 添加变量。"""
    # unreal 的 add_member_variable 需要精确的类型枚举
    type_map = {
        "bool": "bool",
        "int": "int",
        "int32": "int",
        "integer": "int",          # v0.5 清零：与 blueprint_builder VAR_TYPE_MAP 同步
        "float": "float",
        "double": "float",         # v0.5 清零：与 blueprint_builder VAR_TYPE_MAP 同步
        "string": "String",
        "str": "String",           # v0.5 清零：与 blueprint_builder VAR_TYPE_MAP 同步
        "name": "Name",
        "text": "Text",
        "vector": "Vector",
        "rotator": "Rotator",
        "transform": "Transform",
        "color": "LinearColor",   # B1 修复
        "object": "Object",       # B1 修复
        "actor": "Actor",         # B1 修复
        "class": "Class",         # B1 修复
        "boolean": "bool",        # B1 修复（bool 别名）
    }
    ue_type = type_map.get(var_type.lower(), "float")

    # 添加变量
    unreal.BlueprintEditorLibrary.add_member_variable(bp, var_name, ue_type)

    # 设置默认值和属性（通过 CDO — 必须在编译前）
    if var_def.get("default_value"):
        try:
            cd = bp.generated_class().get_default_object()
            if cd:
                setattr(cd, var_name, _parse_value(var_def["default_value"], var_type))
        except:
            pass


# ══════════════════════════════════════════════════════════════
#  变量写入 —— 写后回读验证（T-20260909-USER-DRILL）
# ══════════════════════════════════════════════════════════════
#
# 实测矩阵（2026-09-09 在线 /command add_variable + 回读）：
#   真实写入：bool / int / float / string / vector
#   静默丢弃：name / byte / enum —— 报 success=true 但变量不存在
#
# 根因：C++ Bridge add_member_variable 对不支持的类型静默 no-op 且不抛异常，
#       Python 侧旧代码无条件 append status=ok → 用户看到 success 但变量没写进去
#       （静默谎报，比报错更危险）。修复：写后回读，不在清单即判 failed。
_VAR_TYPE_HINT = {
    "name": "改用 string（FName 变量当前未被 Bridge 接受）",
    "byte": "改用 int（Byte 变量当前未被 Bridge 接受）",
    "uint8": "改用 int",
    "enum": "改用 int 或 string（自定义枚举需先建 UserDefinedEnum 资产，当前不支持）",
}


def _add_variable_verified(bp, var_name: str, var_type: str,
                           var_def: Dict, steps: list) -> bool:
    """添加变量并回读验证；失败时追加 status=failed 的 step 并返回 False。"""
    if BRIDGE_AVAILABLE:
        _safe_call(bridge.add_member_variable, bp, var_name, var_type,
                   error_cat="variable_add")
    else:
        _safe_call(_add_variable_unreal, bp, var_name, var_type, var_def,
                   error_cat="variable_add")

    try:
        written = {v.get("name") for v in _read_variables(bp)}
    except Exception:  # noqa: BLE001  回读不可用 → 不阻断（向后兼容）
        written = None

    if written is not None and var_name not in written:
        hint = _VAR_TYPE_HINT.get(str(var_type).lower(), "")
        steps.append({
            "step": f"add_variable:{var_name}",
            "status": "failed",
            "error": (f"变量 '{var_name}' 未写入：类型 '{var_type}' 未被 UE 接受"
                      f"（静默丢弃）" + (f"；{hint}" if hint else "")),
        })
        return False

    steps.append({"step": f"add_variable:{var_name}", "status": "ok"})
    return True


def _parse_value(val_str: str, var_type: str):
    """解析字符串默认值为 Python 类型。"""
    vt = var_type.lower()
    if vt in ("bool",):
        return val_str.lower() in ("true", "1", "yes")
    if vt in ("int", "int32"):
        return int(val_str)
    if vt == "float":
        return float(val_str)
    return val_str


def _set_component_properties(bp, comp_name: str, properties: Dict):
    """C++ 路径下设置组件模板属性。

    ⚠️ 已废弃（T-20260822-ADDCOMPONENT-ONLINE 裁决 C）：
      真实 UE 中 BPGC.ComponentTemplates 不可达（阶段1实测 AttributeError /
      dir 无该属性）→ 本函数必然静默 return，属性设置静默失效。已改为
      _cmd_add_component C++ 分支 fail-loud 拦截（properties 非空 → 明确报错），
      本函数不再被调用。保留仅为兼容历史引用，勿再用于 C++ 路径。

    T-20260822-ADDCOMPONENT-CPP：C++ AddComponentToBlueprint 只负责 SCS 节点
    注册；属性设置通过 BPGC.ComponentTemplates（UE Python 反射 component_templates）
    定位组件模板后 setattr。找不到模板或设置失败时静默跳过（与降级方案行为一致）。
    """
    try:
        gc = bp.generated_class()
        templates = getattr(gc, "component_templates", None) or []
        comp = None
        for tpl in templates:
            try:
                if str(tpl.get_name()) == comp_name:
                    comp = tpl
                    break
            except Exception:
                continue
        if not comp:
            return
        for prop_name, prop_val in properties.items():
            try:
                setattr(comp, prop_name, prop_val)
            except Exception:
                pass
    except Exception:
        pass


def _add_component_unreal(bp, comp_name: str, comp_class: str, properties: Dict):
    """降级方案：用 unreal Python API 添加组件。"""
    scs = unreal.BlueprintEditorLibrary.get_blueprint_simple_construction_script(bp)
    if not scs:
        raise BridgeError("无法获取 SCS", category="component_add")
    # 通过 SCS 添加组件节点
    comp = unreal.BlueprintEditorLibrary.add_component_to_blueprint(
        bp, comp_class, comp_name
    )
    if not comp:
        raise BridgeError(f"组件添加失败: {comp_class}", category="component_add")
    # 设置属性
    for prop_name, prop_val in properties.items():
        try:
            setattr(comp, prop_name, prop_val)
        except:
            pass


def _add_interface_unreal(bp, interface_name: str):
    """降级方案：用 unreal Python API 添加接口。"""
    iface_class = unreal.load_class(None, f"/Script/Engine.{interface_name}")
    if not iface_class:
        iface_class = unreal.load_class(None, f"/Script/CoreUObject.{interface_name}")
    if not iface_class:
        raise BridgeError(f"接口类未找到: {interface_name}", category="interface_add")
    unreal.BlueprintEditorLibrary.add_implemented_interface(bp, iface_class)


# ══════════════════════════════════════════════════════════
#  POST /connect — 引脚连线（Bridge DLL 三重验证）
# ══════════════════════════════════════════════════════════

class ConnectRequest(BaseModel):
    blueprint_path: str
    # T-20260912-UE5BRIDGE-CONNPIN-GUID：src_node / dst_node 兼容「节点标题」与
    #   「32 位 hex GUID」；标题同名命中 > 1 时 fail-loud 拒绝（绝不猜）。
    connections: List[Dict] = []     # [{src_node, src_pin, dst_node, dst_pin}]

@app.post("/connect")
def connect_pins(req: ConnectRequest, request: Request = None):  # 🔧 B: async→sync def（T-20260806-UE5BRIDGE-CRASH-FIX 方案B·线程池隔离）
    """连线蓝图引脚。

    三重验证（来自 pitfalls.md P01+P02 修复）：
      ① TryCreateConnection(A, B) — 调用 Bridge DLL
      ② 若 !A->LinkedTo.Contains(B) → A->MakeLinkTo(B) 兜底
      ③ 若 !B->LinkedTo.Contains(A) → B->MakeLinkTo(A) 双向保证
      ④ 最终验证 !A->LinkedTo.Contains(B) → Error Log + return false

    Request:
        {
            "blueprint_path": "/Game/Blueprints/BP_Enemy",
            "connections": [
                {"src_node": "BeginPlay", "src_pin": "then",
                 "dst_node": "PrintString", "dst_pin": "execute"}
            ]
        }
    """
    global _request_count
    with _lock:
        _request_count += 1

    # ── Token 认证（N-002 修复）──
    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")

    t0 = time.time()
    # T-20260726-HEALTHFIX #1 A2: 加 'steps' 键（与 connect 端 result schema 对齐）
    result = {"success": True, "results": [], "steps": []}

    if not BRIDGE_AVAILABLE:
        return {
            "success": False,
            "error": "C++ Bridge 插件未加载，连线操作不可用",
            "duration_ms": (time.time() - t0) * 1000,
        }

    try:
        # ── T-20260724-GTFIX: 连线操作改为 GameThread 原子执行 ──
        spec_file = os.path.join(
            os.environ.get("TEMP", "/tmp"),
            f"ue5_connect_spec_{uuid.uuid4().hex[:8]}.json"
        )
        result_file = os.path.join(
            os.environ.get("TEMP", "/tmp"),
            f"ue5_connect_result_{uuid.uuid4().hex[:8]}.json"
        )

        gt_spec = {
            "blueprint_path": req.blueprint_path,
            "connections": req.connections,
        }
        with open(spec_file, "w", encoding="utf-8") as f:
            json.dump(gt_spec, f, ensure_ascii=False)

        # 生成 GameThread 执行脚本（从模板文件加载，N01 修复）
        with open(os.path.join(os.path.dirname(__file__), "game_thread_connect.py.tmpl"), "r", encoding="utf-8") as _f:
            gt_code = _wrap_gt_scope(_f.read().replace("{SPEC_FILE}", repr(spec_file)).replace("{RESULT_FILE}", repr(result_file)))

        # v3.2 实测修复（5.8）：DLL 的 execute_python_on_game_thread 派发路径
        # 在 5.8 引擎的严格 GT 检查下失效（轮询等待期间 GT 被自身阻塞）。
        # 处理器已经 _gt_run_handler 整体分流到 GT → 这里直接执行 Python 路径。
        result.update(_cmd_connect_pins(req.blueprint_path,
                                        req.connections) or {})

        # 清理 spec 文件
        try:
            os.remove(spec_file)
        except Exception:
            pass

        result["duration_ms"] = (time.time() - t0) * 1000

    except BridgeError as e:
        result["success"] = False
        result["error"] = str(e)
        result["duration_ms"] = (time.time() - t0) * 1000
    except Exception as e:
        result["success"] = False
        result["error"] = f"{type(e).__name__}: {e}"
        result["duration_ms"] = (time.time() - t0) * 1000

    return result


def _connect_with_triple_check(bp, src_node_name: str, src_pin_name: str,
                                dst_node_name: str, dst_pin_name: str) -> bool:
    """三重验证连线（来自 pitfalls.md P01+P02 C++ 修复）。

    🔒 B-004 修复：添加 Bridge DLL API 版本检查。

    T-20260912-UE5BRIDGE-CONNPIN-GUID：节点定位升级为 **GUID 精确 / 标题同名 fail-loud**。
      · src_node_name / dst_node_name 兼容「32 位 hex GUID」与「节点标题」两种形态；
      · 传 GUID → `_pywrap_find_graph_by_guid` 精确命中（同名场景唯一可靠锚点）；
      · 传标题且图中命中 > 1 个同名节点 → **拒绝执行**（category=node_ambiguous，
        回传全部同名 GUID 清单），**绝不猜** —— 旧实现只取其一 → 静默连错线；
      · 回读通道不可用 → 退回 `_find_node_by_title_i18n`（向后兼容，不新增失败面）。

    红灯规则：
      - P01: ConnectPins 假阳性 → 验证 A->LinkedTo 和 B->LinkedTo 双方
      - P02: 静默失败 → 每次失败输出 Error Log
      - P03（本单）: 同名节点静默连错线 → 标题歧义 fail-loud 拒绝
    """
    # ── B-004: Bridge DLL 版本检查 ──
    _BRIDGE_MIN_VERSION = "1.0"
    try:
        ver = bridge.get_version() if hasattr(bridge, "get_version") else None
        if ver and ver < _BRIDGE_MIN_VERSION:
            raise BridgeError(
                f"Bridge DLL 版本 {ver} 低于最低要求 {_BRIDGE_MIN_VERSION}",
                category="api_version", pitfall_id="P01"
            )
    except BridgeError:
        raise
    except Exception:
        pass  # get_version() 不存在时静默跳过

    # 查找节点（T-20260912-UE5BRIDGE-CONNPIN-GUID）
    #   GUID 型入参 → GUID 精确；标题型入参 → 同名命中 > 1 时 fail-loud 拒绝
    #   （旧实现只按标题取其一 → 静默连错线，不报错不崩溃，比误删更危险）。
    graph = bridge.get_event_graph(bp)
    if not graph:
        raise BridgeError("无法获取 EventGraph", category="pin_failure",
                          pitfall_id="P01")

    _sg, src_node, src_matched, src_t, src_err, src_cat = _pywrap_locate_node_strict(
        bp, src_node_name, graph=graph)
    if src_node is None:
        if src_cat == "node_ambiguous":
            raise BridgeError(src_err, category="node_ambiguous", pitfall_id="P01",
                              suggestion="改用 read_nodes 返回的 guid 作为 src_node 重试")
        raise BridgeError(f"源节点未找到: {src_node_name}", category="pin_failure")

    _dg, dst_node, dst_matched, dst_t, dst_err, dst_cat = _pywrap_locate_node_strict(
        bp, dst_node_name, graph=graph)
    if dst_node is None:
        if dst_cat == "node_ambiguous":
            raise BridgeError(dst_err, category="node_ambiguous", pitfall_id="P01",
                              suggestion="改用 read_nodes 返回的 guid 作为 dst_node 重试")
        raise BridgeError(f"目标节点未找到: {dst_node_name}", category="pin_failure")

    # 解析后的真实标题（日志 / 拓扑校验用；GUID 入参时回读标题，读不到回显入参）
    src_title = src_t or str(src_node_name)
    dst_title = dst_t or str(dst_node_name)

    # ── 第①重：Bridge DLL ConnectPins ──
    ok = bridge.connect_pins(src_node, src_pin_name, dst_node, dst_pin_name)
    if not ok:
        print(f"[ue5_bridge] [FAIL] ConnectPins 失败: {src_title}.{src_pin_name} → {dst_title}.{dst_pin_name}")
        return False

    # ── 第②重：验证源端 ──
    src_pins = bridge.list_pins(src_node)
    src_pin_obj = None
    for p in src_pins:
        if str(p.pin_name) == src_pin_name:
            src_pin_obj = p
            break
    if src_pin_obj is None:
        print(f"[ue5_bridge] [FAIL] 源引脚未在 list_pins 中找到: {src_title}.{src_pin_name}")
        return False

    # ── 第③重：验证目标端 ──
    dst_pins = bridge.list_pins(dst_node)
    dst_pin_obj = None
    for p in dst_pins:
        if str(p.pin_name) == dst_pin_name:
            dst_pin_obj = p
            break
    if dst_pin_obj is None:
        print(f"[ue5_bridge] [FAIL] 目标引脚未在 list_pins 中找到: {dst_title}.{dst_pin_name}")
        return False

    # ── 第④重：通过 get_node_connections 验证拓扑 ──
    try:
        conns = bridge.get_node_connections(graph)
        found = False
        for c in conns:
            if (str(c.from_node_title) == src_title and
                str(c.from_pin_name) == src_pin_name and
                str(c.to_node_title) == dst_title and
                str(c.to_pin_name) == dst_pin_name):
                found = True
                break
        if not found:
            print(f"[ue5_bridge] [WARN] 连线未在拓扑中确认: {src_title}.{src_pin_name} → {dst_title}.{dst_pin_name}")
            return False
    except Exception as e:
        print(f"[ue5_bridge] [WARN] 连线拓扑验证异常: {e}")

    return True


# ══════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════
#  POST /disconnect — 断开引脚连线（v0.7 新增 · T-20260801-FIX-ENDPOINTS）
# ══════════════════════════════════════════════════════════
# 🔴 KB 语义搜索结果（T-20260801-FIX-ENDPOINTS 编码前 · 2026-08-03）：
#   1. 跨 Pin 操作必须校验同图归属（[已验证] 2026-07-24 ConnectPins C++ 三重修复经验）
#   2. GameThread 内禁止调用 bridge.* 包装函数（AsyncTask 嵌套死锁）→ 直接用
#      unreal.BlueprintPythonBridge.* 静态方法（见 game_thread_connect.py.tmpl 头注）
#   3. C++ 层 Graph 操作前必须调用 Graph->Modify()（真实 UE 下 remove_node 是否
#      已由插件内部处理需 Phase 2 实战验证）

class DisconnectRequest(BaseModel):
    blueprint_path: str
    # T-20260912-UE5BRIDGE-CONNPIN-GUID：node_id 兼容「节点标题」与「32 位 hex GUID」。
    #   ⚠️ 图中存在同名节点（标题命中 > 1）时必须传 GUID，否则端点 fail-loud 拒绝
    #   （回传全部同名节点 GUID，绝不猜 —— 旧实现按标题取其一 → 静默断错线）。
    node_id: str
    pin_name: str


@app.post("/disconnect")
def disconnect_pin(req: DisconnectRequest, request: Request = None):  # 🔧 B: async→sync def（T-20260806-UE5BRIDGE-CRASH-FIX 方案B·线程池隔离）
    """断开指定节点的指定引脚的全部连线。

    v0.7 新增端点（T-20260801-FIX-ENDPOINTS · 修复 R1）。
    设计参考：UE-05-v07-blueprint_editor.md §4.3

    node_id 两种形态（T-20260912-UE5BRIDGE-CONNPIN-GUID）：
      · 32 位 hex GUID（允许 `-` / `{}` 分隔）→ GUID **精确定位**；
      · 节点标题 → 按标题定位；图中**同名节点命中 > 1 时 fail-loud 拒绝执行**
        （回传全部同名节点 GUID 列表，绝不猜）。

    断线策略：
      1. 通过 Bridge get_node_connections_for_node() 枚举目标 pin 全部连线
      2. 跨图同属校验：对端节点必须与目标节点在同一 graph，否则跳过
      3. 逐个 break_link_to() 断开（原生 UE Python API，GameThread 执行）
      4. 通过 get_pin_infos() 验证 is_connected=False
      5. 任何 break_link_to 失败 → 降级 catalog_only（仅记录未真断）

    Returns:
        {
            "success": true,
            "data": {
                "node": "BeginPlay", "pin": "then",
                "broken_links": [{"from": ..., "from_pin": ..., "to": ..., "to_pin": ...}],
                "verified_disconnected": true,
                "method": "python_api" | "catalog_only"
            },
            "detail": "..."
        }
    """
    global _request_count
    with _lock:
        _request_count += 1

    # ── Token 认证（N-002 修复）──
    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")

    t0 = time.time()

    # ── Bridge 不可用 → 直接降级 catalog_only（L1 降级路径）──
    if not BRIDGE_AVAILABLE:
        return {
            "success": True,
            "data": {
                "node": req.node_id, "pin": req.pin_name,
                "broken_links": [], "verified_disconnected": False,
                "method": "catalog_only",
            },
            "detail": "C++ Bridge 未加载，降级到 catalog_only（仅记录未真断）",
            "duration_ms": (time.time() - t0) * 1000,
        }

    try:
        bp = _resolve_blueprint(req.blueprint_path)
    except BridgeError as e:
        return {
            "success": False,
            "data": {
                "node": req.node_id, "pin": req.pin_name,
                "broken_links": [], "verified_disconnected": False,
                "method": "catalog_only",
            },
            "detail": str(e),
            "category": e.category,
            "duration_ms": (time.time() - t0) * 1000,
        }

    try:
        payload = _run_on_game_thread_sync(
            _disconnect_pin_on_game_thread, bp, req.node_id, req.pin_name,
            timeout=30.0,
        )
    except BridgeError as e:
        payload = {
            "success": False,
            "data": {
                "node": req.node_id, "pin": req.pin_name,
                "broken_links": [], "verified_disconnected": False,
                "method": "catalog_only",
            },
            "detail": str(e),
            "category": e.category,
        }
    except Exception as e:
        payload = {
            "success": False,
            "data": {
                "node": req.node_id, "pin": req.pin_name,
                "broken_links": [], "verified_disconnected": False,
                "method": "catalog_only",
            },
            "detail": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-500:],
        }

    payload["duration_ms"] = (time.time() - t0) * 1000
    return payload

def _find_graph_of_node(bp, node_id: str):
    """在蓝图所有图中查找包含指定节点的图（跨图同属校验前置）。

    ⚠️ 在 GameThread 内调用，只能用 unreal.BlueprintPythonBridge.* 静态方法。
    """
    BPB = unreal.BlueprintPythonBridge
    # T-20260912-UE5BRIDGE-CONNPIN-GUID：GUID 型入参 → GUID 精确定位（不依赖标题，
    #   同名场景天然唯一）；标题型入参保持既有语义（向后兼容）。
    if _pywrap_is_guid_like(node_id):
        _g_guid, _n_guid, _e_guid = _pywrap_find_graph_by_guid(bp, node_id)
        return _g_guid
    # ── F-4（T-20260912-UE5BRIDGE-TITLEGATE-FIX）：标题探针 → **GUID 计数定位** ──
    #   旧实现逐图裸调 `find_node_by_title`，命中即返回**第一张图**：同名节点
    #   跨图存在时会静默锁定错误的图（随后图内计数为 1 → 误判「唯一命中」→ 误操作）。
    #   现行：逐图按 `_pywrap_title_matches` 计数并**跨图累计**：
    #     · 全局恰 1 → 返回该图；
    #     · 全局 > 1 → **fail-loud**（category=node_ambiguous + 全部同名 GUID/图名清单）；
    #     · 全局 0 且回读可用 → None（确无此节点）；
    #     · 回读通道不可用 → 退回旧标题探针（不新增失败面）。
    graphs = _all_blueprint_graphs(bp)
    _hits = []          # [(graph, info)] 跨图命中累计
    _readback_ok = False
    for g in graphs:
        if g is None:
            continue
        for _cand in _pywrap_title_candidates(node_id):
            try:
                _matches, _merr = _pywrap_title_matches(g, _cand)
            except Exception:
                _matches = None
            if _matches is None:
                continue
            _readback_ok = True
            if _matches:
                for _info in _matches:
                    _hits.append((g, _info))
                break   # 同图同节点只计一次（i18n 候选变体不重复计数）
    if _readback_ok:
        if len(_hits) == 1:
            return _hits[0][0]
        if len(_hits) > 1:
            _dups = [{"graph": (g.get_name() if hasattr(g, "get_name") else "?"),
                      "guid": (i.guid or "<GUID不可读>"),
                      "class": (i.node_class or "?"),
                      "pos": [i.pos_x, i.pos_y]} for g, i in _hits]
            raise BridgeError(
                "节点标题歧义，拒绝执行: 「%s」在蓝图内命中 %d 个同名节点 → "
                "按标题定位会误操作且无法确定目标，请改用 GUID 精确指定。"
                "同名节点清单: %s" % (node_id, len(_hits), _dups),
                category="node_ambiguous")
        return None
    # 回读通道不可用 → 退回旧标题探针（仅此一处允许裸 find_node_by_title）
    for g in graphs:
        if g is None:
            continue
        try:
            if BPB.find_node_by_title(g, node_id) is not None:
                return g
        except Exception:
            continue
    return None


def _all_blueprint_graphs(bp):
    """枚举蓝图全部图（失败降级 EventGraph；再失败空表）。"""
    BPB = unreal.BlueprintPythonBridge
    try:
        return list(BPB.get_all_graphs(bp) or [])
    except Exception:
        try:
            return [BPB.get_event_graph(bp)]
        except Exception:
            return []


def _pywrap_title_matches(graph, title: str):
    """枚举图中标题**精确等于** title 的节点 → (list[_PyWrapNodeInfo] | None, err)。

    返回 `None` = 回读通道不可用（`list_nodes` 异常）→ 调用方退回旧「按标题唯一
    查找」行为（不新增失败面）；返回 `[]` = 确实没有该标题的节点。

    为什么需要「计数」：`find_node_by_title` 只返回**一个**节点，同名场景下无法
    区分「唯一命中」与「多命中之一」→ 旧删除路径因此误删（见 §E3 头注）。
    """
    infos, err = _pywrap_readback_nodes(graph)
    if err:
        return None, err
    return [i for i in infos if (i.title or "") == (title or "")], ""


def _pywrap_find_graph_by_guid(bp, guid: str):
    """按 GUID 在蓝图全部图中定位节点 → (graph, node, err_str)。

    T-20260911-UE5BRIDGE-DELNODE-GUID 新增（GUID 精确删除路径的定位原语）。
    与 `_find_graph_of_node`（按标题）不同，本函数**不依赖标题** → 同名节点场景
    下天然唯一（GUID 全局唯一）。GUID 经 `_pywrap_norm_guid` 归一化后比较。

    定位双通道：① C++ `GetNodeByGuid`（首选）；② `list_nodes` 回读匹配 GUID 后
   再用 `GetNodeByGuid` 取对象（H-1 已知：裸节点 `node_guid` 不可达，但
    `list_nodes` 的 `BPNodeInfo.node_guid` 可达）。
    """
    BPB = unreal.BlueprintPythonBridge
    target = _pywrap_norm_guid(guid)
    if not target:
        return None, None, "GUID 为空"
    if not hasattr(BPB, "get_node_by_guid"):
        return None, None, "C++ GetNodeByGuid 不可用（Bridge 插件未加载）"
    for g in _all_blueprint_graphs(bp):
        if g is None:
            continue
        n = None
        try:
            n = BPB.get_node_by_guid(g, target)
        except Exception:
            n = None
        if n is None:
            try:
                infos, _e = _pywrap_readback_nodes(g)
            except Exception:
                infos = []
            for i in infos:
                if i.guid and i.guid == target:
                    try:
                        n = BPB.get_node_by_guid(g, i.guid)
                    except Exception:
                        n = None
                    break
        if n is not None:
            return g, n, ""
    return None, None, "GUID 未命中任何图"


def _pywrap_title_candidates(title: str) -> List[str]:
    """标题 → 候选标题列表（原文 + i18n 双向变体，去重保序）。

    T-20260912-UE5BRIDGE-CONNPIN-GUID：UE 中文环境下节点标题会被本地化
    （PrintString → 打印字符串），调用方两种写法都要能命中；与
    `_find_node_by_title_i18n` / `_find_bp_node_info` 共用 `_NODE_TITLE_I18N`。
    """
    out = []
    t = str(title or "").strip()
    if t:
        out.append(t)
    for eng, variants in _NODE_TITLE_I18N.items():
        if eng == t:
            for v in variants:
                if v not in out:
                    out.append(v)
        elif t in variants:
            if eng not in out:
                out.append(eng)
            for v in variants:
                if v not in out:
                    out.append(v)
    return out


def _pywrap_readback_title_of(graph, guid_ref):
    """回读表按 GUID 反查节点真实标题（T-20260912-UE5BRIDGE-CONNPIN-GUID）。

    ⚠️ 在线实测（2026-09-12 00:5x）：`get_node_by_guid()` 返回的 K2Node 对象上
    `_pywrap_node_title()` 取到的是**空串** —— `node_title` 只存在于 C++ `list_nodes`
    的回读结构体上，K2Node 自身不暴露该属性。若把空串当标题用于第④重拓扑校验
    （`c.from_node_title == src_title`），会**假阴性 → 误判连线失败**（本单实测踩中）。
    且 K2Node 亦**不暴露可反查自身的 GUID 属性**（实测返回空串）—— 故改走
    「**入参 GUID → 回读表反查标题**」：既不依赖 Python 绑定细节，也天然精确。
    """
    if graph is None:
        return ""
    try:
        target = _pywrap_norm_guid(guid_ref)
    except Exception:
        return ""
    if not target:
        return ""
    try:
        infos, _e = _pywrap_readback_nodes(graph)
    except Exception:
        return ""
    for i in infos or []:
        if i.guid == target:
            return i.title or ""
    return ""


def _pywrap_locate_node_strict(bp, ref, graph=None):
    """按 GUID 或标题**精确定位**节点 → (graph, node, matched_by, matched_title, err, err_cat)。

    matched_title = 解析出的**真实回读标题**（GUID 入参时由回读表反查；标题入参时为命中
    的那个候选标题）。⚠️ 必须回传该值：K2Node 对象上取不到 node_title（实测为空串），
    调用方拿对象再问标题会得到空串 → 拓扑校验假阴性。

    T-20260912-UE5BRIDGE-CONNPIN-GUID：连接/断线路径旧的标题定位会**静默连错线**
    （同名节点命中 > 1 时 `find_node_by_title` 只返回其中一个且不报错——比误删更
    危险：不崩溃、不报错、结果静默错误）。本原语：

      · GUID 型入参 → `_pywrap_find_graph_by_guid` 精确命中（GUID 全局唯一）；
      · 标题型入参 → `_pywrap_title_matches` **计数**：
          - 命中 1 → 以其 GUID 取对象（取不到再退回 find_node_by_title）
          - 命中 > 1 → **fail-loud 拒绝**（回传全部同名节点 GUID，绝不猜）
          - 命中 0 → 试 i18n 候选变体；全试完仍 0 → 节点不存在
      · 回读通道不可用（`list_nodes` 异常 → matches is None）→ **退回旧行为**
        （`_find_node_by_title_i18n` 唯一查找），不新增失败面。

    err 非空 = 调用方**必须放弃操作**（不得降级成「随便挑一个」）。
    graph 非空时只在该图内查找（保持「/connect 只在 EventGraph」的既有语义）。
    """
    BPB = unreal.BlueprintPythonBridge
    raw = str(ref or "").strip()
    if not raw:
        return (None, None, "", "",
                "节点不存在: <空参数>（需给节点标题或 32 位 GUID）", "node_failure")

    # ── 路径 A：GUID 精确（同名场景下唯一可靠锚点）──
    if _pywrap_is_guid_like(raw):
        guid = _pywrap_norm_guid(raw)
        g, n, gerr = _pywrap_find_graph_by_guid(bp, guid)
        if g is None or n is None:
            return (None, None, "", "",
                    f"节点不存在（GUID 未命中）: {raw} — {gerr}", "node_failure")
        return g, n, "guid", _pywrap_readback_title_of(g, guid), "", ""

    # ── 路径 B：标题定位（向后兼容 + 同名 fail-loud）──
    graphs = [graph] if graph is not None else _all_blueprint_graphs(bp)
    for g in graphs:
        if g is None:
            continue
        for cand in _pywrap_title_candidates(raw):
            try:
                matches, _merr = _pywrap_title_matches(g, cand)
            except Exception:
                matches = None
            if matches is None:
                # 回读通道不可用 → 退回旧行为（唯一标题查找，i18n 感知）
                try:
                    old = _find_node_by_title_i18n(g, raw)
                except Exception:
                    old = None
                if old is not None:
                    return (g, old, "title", (raw or ""), "", "")
                continue
            if len(matches) > 1:
                dups = [{"guid": (i.guid or "<GUID不可读>"),
                         "class": (i.node_class or "?"),
                         "pos": [i.pos_x, i.pos_y]} for i in matches]
                return (None, None, "", "",
                        "节点标题歧义，拒绝执行: 「%s」在图中命中 %d 个同名节点 → "
                        "按标题连线/断线会连错目标且无法确定意图，请改用 GUID 精确指定。"
                        "同名节点清单: %s" % (raw, len(matches), dups),
                        "node_ambiguous")
            if len(matches) == 1:
                info = matches[0]
                n = None
                if info.guid:
                    try:
                        n = BPB.get_node_by_guid(g, info.guid)
                    except Exception:
                        n = None
                if n is None:
                    n = BPB.find_node_by_title(g, cand)
                if n is None:
                    n = _find_node_by_title_i18n(g, raw)
                if n is not None:
                    return g, n, "title", (info.title or cand), "", ""
    return None, None, "", "", f"节点不存在: {raw}", "node_failure"


def _pywrap_find_peer_in_graph(graph, title):
    """对端节点按标题定位 → (node | None, err_str)。

    err 非空 = 对端标题歧义（同名 > 1）→ 调用方必须**拒绝该条连线操作**
    （记入 failed/skipped 并降级 catalog_only），绝不猜。
    err 空 + node is None = 不在本图 → 既有「跨图同属校验失败」跳过语义。
    """
    BPB = unreal.BlueprintPythonBridge
    t = str(title or "")
    try:
        matches, _merr = _pywrap_title_matches(graph, t)
    except Exception:
        matches = None
    if matches is None:
        # 兜底①（唯一允许的裸标题定位之一）：回读通道不可用（list_nodes 异常）→
        #   GUID/计数均不可得，只能退回唯一标题查找（**不新增失败面**，与
        #   CONNPIN-GUID 既有语义一致）；此时同名歧义无法校验 → 记 warning。
        try:
            _n1 = BPB.find_node_by_title(graph, t)
        except Exception:
            _OP_LOG.warning(f"对端节点定位失败（回读不可用 + find_node_by_title 异常）: {t}")
            return None, ""
        if _n1 is not None:
            _OP_LOG.warning(
                f"对端节点「{t}」回读通道不可用 → 退回唯一标题查找（同名歧义无法校验）")
        return _n1, ""
    if len(matches) == 0:
        return None, ""
    if len(matches) > 1:
        dups = [i.guid or "<GUID不可读>" for i in matches]
        return None, ("对端节点标题歧义（命中 %d 个同名节点），拒绝该条连线操作: "
                      "「%s」→ %s" % (len(matches), t, dups))
    info = matches[0]
    n = None
    if info.guid:
        try:
            n = BPB.get_node_by_guid(graph, info.guid)
        except Exception:
            n = None
    if n is None:
        # 兜底②（唯一允许的裸标题定位之二）：命中恰 **1** 个（len(matches)==1）且
        #   该节点 GUID 不可读/取对象失败 → 唯一命中下标题查找无歧义，不构成「猜」。
        n = BPB.find_node_by_title(graph, t)
    return n, ""



def _pywrap_verify_pin_disconnected(graph, node_title, pin_name):
    """第 4 重校验：用 `get_pin_infos(graph, **BPNodeInfo**)` 确认目标 pin 已无连线。

    T-20260912-UE5BRIDGE-TITLEGATE-FIX（F-6）：C++ 签名为
    `GetPinInfos(UEdGraph*, const FBPNodeInfo&)` —— 传 K2Node 会抛 **TypeError**
    （见 `_cmd_set_pin_default_value` 头注的在线实测记录）。若把 K2Node 传进去，
    异常被裸 `except` 吞掉 → 校验形同虚设、结果**静默降级 catalog_only**。
    本函数统一走 **BPNodeInfo**（`list_nodes` 回读结构体 `_PyWrapNodeInfo.raw`），
    并把「校验通道不可用」与「确实仍连着」严格区分：
      · True  = 目标 pin 已无连线（校验通过）
      · False = 目标 pin 仍有连线（应降级 catalog_only）
      · None  = 校验通道不可用（**不据此降级**，保持既有结论，仅告警）
    """
    if not pin_name:
        return None
    try:
        infos, _e = _pywrap_readback_nodes(graph)
    except Exception as e:
        _OP_LOG.warning(f"/disconnect 第4重校验回读不可用: {e}")
        return None
    _same = [i for i in (infos or []) if (i.title or "") == (node_title or "")]
    if len(_same) != 1:
        # 0 个（找不到）或 >1（同名歧义）→ 无法安全校验，返回 None 不降级
        return None
    if not hasattr(unreal.BlueprintPythonBridge, "get_pin_infos"):
        return None
    try:
        # ⚠️ 必须传 BPNodeInfo（info.raw）；传 K2Node → TypeError（签名不匹配）
        pins = unreal.BlueprintPythonBridge.get_pin_infos(graph, _same[0].raw) or []
    except TypeError as e:
        _OP_LOG.warning(
            f"/disconnect 第4重校验 get_pin_infos 类型不匹配（须传 BPNodeInfo）: {e}")
        return None
    except Exception as e:
        _OP_LOG.warning(f"/disconnect 第4重校验通道不可用: {e}")
        return None
    for p in pins:
        if str(getattr(p, "pin_name", "")) != pin_name:
            continue
        try:
            return not bool(getattr(p, "is_connected", False))
        except Exception:
            return None
    return None


def _pin_owner_node(pin):
    """获取 pin 的所属节点（兼容 fake 替身与真实 UE Python API）。"""
    owner = getattr(pin, "_owner", None)
    if owner is not None:
        return owner
    for m in ("get_owning_node", "get_owning_node_unchecked"):
        try:
            fn = getattr(pin, m, None)
            if fn is not None:
                return fn()
        except Exception:
            pass
    return None


def _disconnect_pin_on_game_thread(bp, node_id: str, pin_name: str) -> Dict:
    """GameThread 上执行断线（供 /disconnect 端点调用）。

    设计（UE-05-v07-blueprint_editor.md §4.3）：
      1. 枚举目标 pin 的全部连线（Bridge get_node_connections_for_node）
      2. 跨图同属校验：对端节点必须与目标节点在同一 graph，否则跳过
      3. 逐个 break_link_to() 断开（原生 UE Python API）
      4. 验证同图剩余连线为 0（跨图跳过是预期行为，不算失败）
      5. 任何 break_link_to 失败 → 降级 catalog_only（仅记录未真断）

    T-20260912-UE5BRIDGE-CONNPIN-GUID：`node_id` 兼容「32 位 hex GUID」与「节点标题」。
      · 传 GUID → GUID 精确定位（同名场景唯一可靠锚点）；
      · 传标题且命中 > 1 个同名节点 → **fail-loud 拒绝**（node_ambiguous + 同名 GUID 清单），
        **绝不猜**（旧实现 find_node_by_title 只取其一 → 静默断错线）；
      · 对端节点（连线另一端）标题同名 > 1 → 该条连线操作拒绝并降级 catalog_only；
      · 回读通道不可用 → 退回旧标题查找（向后兼容，不新增失败面）。

    ⚠️ 本函数在 GameThread（tick worker）中执行，禁止调用 bridge.* 包装函数
       （其内部 AsyncTask 会嵌套死锁），直接用 unreal.BlueprintPythonBridge.*
       静态方法与原生 unreal Python API。
    """
    BPB = unreal.BlueprintPythonBridge

    # ── 找到节点所在图 + 节点（T-20260912-UE5BRIDGE-CONNPIN-GUID）──
    #   GUID 型入参 → GUID 精确；标题型 → 同名命中 > 1 时 fail-loud 拒绝
    #   （旧实现 find_node_by_title 只取其一 → 静默断错线 / 连错线）；
    #   回读通道不可用 → 退回旧标题查找（不新增失败面）。
    _raw_id = str(node_id or "").strip()
    graph, node, _matched_by, _mt, _lerr, _lcat = _pywrap_locate_node_strict(bp, _raw_id)
    if graph is None or node is None:
        raise BridgeError(_lerr or f"节点不存在: {_raw_id}",
                          category=(_lcat or "node_failure"))
    node_id = _mt or _raw_id   # 后续连线枚举按真实标题比对

    # ── 枚举目标 pin 的连线 ──
    conns = []
    try:
        conns = BPB.get_node_connections_for_node(graph, node) or []
    except Exception:
        try:
            all_conns = BPB.get_node_connections(graph) or []
            conns = [c for c in all_conns
                     if (str(getattr(c, "from_node_title", "")) == node_id or
                         str(getattr(c, "to_node_title", "")) == node_id)]
        except Exception:
            conns = []
    target_conns = [
        c for c in conns
        if (str(getattr(c, "from_node_title", "")) == node_id and
            str(getattr(c, "from_pin_name", "")) == pin_name) or
           (str(getattr(c, "to_node_title", "")) == node_id and
            str(getattr(c, "to_pin_name", "")) == pin_name)
    ]

    if not target_conns:
        return {
            "success": True,
            "data": {
                "node": node_id, "pin": pin_name,
                "broken_links": [], "already_disconnected": True,
                "verified_disconnected": True, "method": "python_api",
            },
            "detail": f"{node_id}.{pin_name} 本就无连接",
        }

    # ── 优先路径：C++ BreakPins（v0.8 新插件 API，UE 重启后生效）──
    # T-20260805 Bug2 扩展：真实 UE 5.1 Python 无 EdGraphPin 绑定（node.pins /
    #   pin.break_link_to 均不存在），断线只能由 C++ 层实现（UEdGraphPin::BreakLinkTo）。
    if hasattr(BPB, "break_pins"):
        broken_links = []
        skipped_cross_graph = []
        failed_break = []
        for c in target_conns:
            from_title = str(getattr(c, "from_node_title", ""))
            from_pin = str(getattr(c, "from_pin_name", ""))
            to_title = str(getattr(c, "to_node_title", ""))
            to_pin = str(getattr(c, "to_pin_name", ""))
            # T-20260912-UE5BRIDGE-CONNPIN-GUID：对端节点按标题定位 → 同名歧义
            #   必须拒绝该条（绝不猜），否则静默断错线
            if from_title == node_id:
                src_obj, src_pin = node, from_pin
                dst_obj, _peer_err = _pywrap_find_peer_in_graph(graph, to_title)
                dst_pin = to_pin
            else:
                src_obj, _peer_err = _pywrap_find_peer_in_graph(graph, from_title)
                src_pin = from_pin
                dst_obj, dst_pin = node, to_pin
            if _peer_err:
                failed_break.append({
                    "from": from_title, "from_pin": from_pin,
                    "to": to_title, "to_pin": to_pin, "reason": _peer_err,
                })
                continue
            if not src_obj or not dst_obj:
                skipped_cross_graph.append({
                    "to_node": to_title, "to_pin": to_pin,
                    "reason": "对端节点不在同一图中（跨图同属校验失败）",
                })
                continue
            try:
                ok = BPB.break_pins(src_obj, src_pin, dst_obj, dst_pin)
                if ok:
                    broken_links.append({
                        "from": from_title, "from_pin": from_pin,
                        "to": to_title, "to_pin": to_pin,
                    })
                else:
                    failed_break.append({
                        "from": from_title, "from_pin": from_pin,
                        "to": to_title, "to_pin": to_pin, "reason": "BreakPins 返回 False",
                    })
            except Exception as e:
                failed_break.append({
                    "from": from_title, "from_pin": from_pin,
                    "to": to_title, "to_pin": to_pin, "reason": str(e),
                })

        verified = not failed_break
        method = "python_api" if verified else "catalog_only"
        if failed_break:
            _OP_LOG.warning(f"/disconnect BreakPins 部分失败 → catalog_only: {failed_break}")
        return {
            "success": True,
            "data": {
                "node": node_id, "pin": pin_name,
                "broken_links": broken_links,
                "verified_disconnected": verified,
                "method": method,
                "skipped_cross_graph": skipped_cross_graph,
            },
            "detail": (
                f"{node_id}.{pin_name} 断开 {len(broken_links)} 条连线（C++ BreakPins）"
                if verified
                else f"{node_id}.{pin_name} 降级到 catalog_only（BreakPins 失败 {len(failed_break)} 条）"
            ),
        }

    # ── 旧路径（fake 环境 / 旧插件）：node.pins + break_link_to ──
    # 真实 UE 5.1 的 K2Node 无 pins 属性 → 会走到下方"引脚不存在"报错，属预期
    #（真实断线依赖 C++ BreakPins API，需 UE 重启加载新插件后生效）。
    target_pin = None
    try:
        for p in node.pins:
            if str(p.pin_name) == pin_name:
                target_pin = p
                break
    except Exception:
        target_pin = None
    if target_pin is None:
        raise BridgeError(f"引脚不存在: {node_id}.{pin_name}", category="pin_failure")

    # ── 逐个断开（跨图同属校验）──
    broken_links = []
    skipped_cross_graph = []
    method = "python_api"

    linked_pins = list(getattr(target_pin, "linked_to", None) or [])
    for other_pin in linked_pins:
        other_node = _pin_owner_node(other_pin)
        other_title = str(getattr(other_node, "node_title", "?"))

        # 跨图同属校验：对端节点必须在同一 graph（KB 命中：跨图 Pin 操作必须校验同图归属）
        # T-20260912-UE5BRIDGE-CONNPIN-GUID：对端同名歧义 → 拒绝该条（绝不猜）
        _peer_node, _peer_err = _pywrap_find_peer_in_graph(graph, other_title)
        if _peer_err:
            method = "catalog_only"
            skipped_cross_graph.append({
                "to_node": other_title,
                "to_pin": str(getattr(other_pin, "pin_name", "?")),
                "reason": _peer_err,
            })
            continue
        same_graph = _peer_node is not None
        if not same_graph:
            skipped_cross_graph.append({
                "to_node": other_title,
                "to_pin": str(getattr(other_pin, "pin_name", "?")),
                "reason": "跨图同属校验失败：对端节点不在同一图中",
            })
            continue

        try:
            target_pin.break_link_to(other_pin)
            broken_links.append({
                "from": node_id, "from_pin": pin_name,
                "to": other_title, "to_pin": str(getattr(other_pin, "pin_name", "?")),
            })
        except Exception as e:
            method = "catalog_only"
            _OP_LOG.warning(f"/disconnect break_link_to 失败 → catalog_only: {e}")

    # ── 验证：同图连接已全部断开（跨图跳过是预期行为，不算失败）──
    verified = True
    if method != "python_api":
        # break_link_to 已发生失败 → 未验证通过
        verified = False
    else:
        try:
            remaining_same_graph = 0
            for other_pin in list(getattr(target_pin, "linked_to", None) or []):
                other_node = _pin_owner_node(other_pin)
                other_title = str(getattr(other_node, "node_title", "?"))
                _peer_v, _peer_v_err = _pywrap_find_peer_in_graph(
                    graph, other_title)
                if _peer_v_err or _peer_v is not None:
                    remaining_same_graph += 1
            if remaining_same_graph > 0:
                verified = False
                method = "catalog_only"
        except Exception:
            pass

    # ── 第 4 重校验（F-6）：get_pin_infos(graph, BPNodeInfo) 确认已无连线 ──
    #   ⚠️ 传 K2Node 会 TypeError（C++ 签名要求 FBPNodeInfo）→ 统一走回读结构体；
    #   「通道不可用」(None) 不降级、「确实仍连着」(False) 才降级——不静默吞异常。
    #   ⚠️ 有跳过项（跨图同属 / 对端歧义）时 pin 本就应保持连接 → 校验不可结论化，
    #      仅在「全部同图连线均已处理」时才做该重校验（否则会误伤既有 skip 语义）。
    if method == "python_api" and not skipped_cross_graph:
        _v4 = _pywrap_verify_pin_disconnected(graph, node_id, pin_name)
        if _v4 is False:
            verified = False
            method = "catalog_only"
            _OP_LOG.warning(
                f"/disconnect 第4重校验发现 {node_id}.{pin_name} 仍有连线 → catalog_only")

    return {
        "success": True,
        "data": {
            "node": node_id, "pin": pin_name,
            "broken_links": broken_links,
            "verified_disconnected": verified,
            "method": method,
            "skipped_cross_graph": skipped_cross_graph,
        },
        "detail": (
            f"{node_id}.{pin_name} 断开 {len(broken_links)} 条连线"
            if method == "python_api"
            else f"{node_id}.{pin_name} 降级到 catalog_only（break_link_to 失败，仅记录未真断）"
        ),
    }

# ══════════════════════════════════════════════════════════
#  POST /delete_node — 删除节点（v0.7 新增 · T-20260801-FIX-ENDPOINTS）
# ══════════════════════════════════════════════════════════

class DeleteNodeRequest(BaseModel):
    blueprint_path: str
    # T-20260911-UE5BRIDGE-DELNODE-GUID：node_id 兼容「节点标题」与「32 位 hex GUID」。
    #   ⚠️ 图中存在同名节点（标题命中 > 1）时必须传 GUID，否则端点 fail-loud 拒绝。
    node_id: str
    dry_run: bool = False


@app.post("/delete_node")
def delete_node(req: DeleteNodeRequest, request: Request = None):  # 🔧 B: async→sync def（T-20260806-UE5BRIDGE-CRASH-FIX 方案B·线程池隔离）
    """删除蓝图中的指定节点（三步删除 + 引用检查 + 干跑）。

    v0.7 新增端点（T-20260801-FIX-ENDPOINTS · 修复 R2）。
    设计参考：UE-05-v07-blueprint_editor.md §4.4

    删除三步（2026-08-20 实测核对 · T-20260820-REMOVE-NODE-KB）：
      1. break_link_to() 逐个断开所有连线（跨图同属校验）
      2. C++ BlueprintPythonBridge.remove_node(graph, node) 优先；
         降级 remove_unused_nodes（仅删未连接节点；有连线时诚实报错）
         ⚠️ 实测确认：graph.remove_node / BlueprintEditorLibrary.remove_graph_node /
            node.destroy_node 在 UE 5.1 Python 绑定均不存在（AttributeError）
      3. 标记蓝图脏：bp.mark_package_dirty()

    node_id 两种形态（T-20260911-UE5BRIDGE-DELNODE-GUID）：
      · 32 位 hex GUID（允许 `-` / `{}` 分隔）→ GUID **精确删除**；
      · 节点标题 → 按标题定位；图中**同名节点命中 > 1 时 fail-loud 拒绝执行**
        （回传全部同名节点 GUID 列表，绝不猜），dry_run 同样拒绝。
      ⚠️ 旧版只按标题定位 → 同名场景实测误删（A/B MISDELETE_OBSERVED=true）。

    dry_run：仅检查引用关系（统计将被断开的连线）+ 定位判定，不实际删除。

    Returns:
        {
            "success": true,
            "data": {
                "deleted_node": "PrintString",
                "deleted_guid": "<32 位 hex；不可读时为空串>",
                "matched_by": "title | guid",
                "broken_links": 2,
                "removed_from_graph": true,
                "destroyed": false,
                "marked_dirty": true,
                "method": "python_api"
            },
            "detail": "..."
        }
    """
    global _request_count
    with _lock:
        _request_count += 1

    # ── Token 认证（N-002 修复）──
    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")

    t0 = time.time()

    if not BRIDGE_AVAILABLE:
        return {
            "success": False,
            "data": {"deleted_node": req.node_id, "broken_links": 0,
                     "removed_from_graph": False, "destroyed": False,
                     "marked_dirty": False, "method": "python_api"},
            "detail": "C++ Bridge 插件未加载，删除操作不可用",
            "duration_ms": (time.time() - t0) * 1000,
        }

    try:
        bp = _resolve_blueprint(req.blueprint_path)
    except BridgeError as e:
        return {
            "success": False,
            "data": {"deleted_node": req.node_id, "broken_links": 0,
                     "removed_from_graph": False, "destroyed": False,
                     "marked_dirty": False, "method": "python_api"},
            "detail": str(e),
            "category": e.category,
            "duration_ms": (time.time() - t0) * 1000,
        }

    try:
        payload = _run_on_game_thread_sync(
            _delete_node_on_game_thread, bp, req.node_id, req.dry_run,
            timeout=30.0,
        )
    except BridgeError as e:
        payload = {
            "success": False,
            "data": {"deleted_node": req.node_id, "broken_links": 0,
                     "removed_from_graph": False, "destroyed": False,
                     "marked_dirty": False, "method": "python_api"},
            "detail": str(e),
            "category": e.category,
        }
    except Exception as e:
        payload = {
            "success": False,
            "data": {"deleted_node": req.node_id, "broken_links": 0,
                     "removed_from_graph": False, "destroyed": False,
                     "marked_dirty": False, "method": "python_api"},
            "detail": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-500:],
        }

    payload["duration_ms"] = (time.time() - t0) * 1000
    return payload

def _delete_node_on_game_thread(bp, node_id: str, dry_run: bool = False) -> Dict:
    """GameThread 上执行节点删除（供 /delete_node 端点调用）。

    入参 `node_id` 兼容两种形态（T-20260911-UE5BRIDGE-DELNODE-GUID）：
      · **32 位 hex GUID**（允许 `-` / `{}` 分隔）→ 按 GUID **精确删除**；
      · **节点标题**（其余形态）→ 按标题定位，且**同名歧义时 fail-loud 拒绝**。

    ⚠️ 本任务根因（为什么要改）：旧实现由 `_find_graph_of_node` +
       `find_node_by_title` **只按标题定位**，同名（同 title 异 GUID）场景下无法
       区分目标 → 实测误删先存在的合法节点（A/B 对照 `MISDELETE_OBSERVED=true`，
       见 `output/T-20260910-UE5BRIDGE-CLEANUP-GUID/阶段2-在线验证报告-20260911.md` §1.1）。
       现行策略：标题命中数 > 1 → **绝不猜**，拒绝执行并回传全部同名节点 GUID。

    删除三步（2026-08-20 实测核对 · T-20260820-REMOVE-NODE-KB）：
      1. 枚举并断开所有连线（跨图同属校验：只断同图连线）
      2. 从图中移除节点：
         · 已拿到真 GUID → `_pywrap_delete_node_by_guid`（`GetNodeByGuid` +
           C++ `RemoveNode`；与 H-2 残留清理**同一原语**，同源精确）
         · 无 GUID（回读不可用）/ 无 C++ RemoveNode → 旧 `remove_node` /
           `remove_unused_nodes` 降级路径（向后兼容）
         ⚠️ 实测确认：graph.remove_node / BlueprintEditorLibrary.remove_graph_node /
            node.destroy_node 在 UE 5.1 Python 绑定均不存在（AttributeError）
      3. 标记蓝图脏

    dry_run：仅检查引用 + 定位判定，不实际删除（**同名歧义同样 fail-loud**）。

    ⚠️ 本函数在 GameThread（tick worker）中执行，禁止调用 bridge.* 包装函数
       （其内部 AsyncTask 会嵌套死锁），直接用 unreal.BlueprintPythonBridge.*
       静态方法与原生 unreal Python API。
    """
    BPB = unreal.BlueprintPythonBridge

    # ── Step 0：入参形态判定 → 定位（图 + 节点）──
    raw_arg = str(node_id or "").strip()
    if not raw_arg:
        raise BridgeError("节点不存在: <空参数>（需给节点标题或 32 位 GUID）",
                          category="node_failure")

    matched_by = ""
    node_guid = ""

    if _pywrap_is_guid_like(raw_arg):
        # ── 路径 A：GUID 精确删除（同名场景下唯一可靠锚点）──
        node_guid = _pywrap_norm_guid(raw_arg)
        graph, node, gerr = _pywrap_find_graph_by_guid(bp, node_guid)
        if graph is None or node is None:
            raise BridgeError(
                f"节点不存在（GUID 未命中）: {raw_arg} — {gerr}",
                category="node_failure")
        matched_by = "guid"
    else:
        # ── 路径 B：按标题定位（向后兼容 + 同名 fail-loud）──
        graph = _find_graph_of_node(bp, raw_arg)
        if graph is None:
            raise BridgeError(f"节点不存在: {raw_arg}", category="node_failure")
        matches, _merr = _pywrap_title_matches(graph, raw_arg)
        if matches is None:
            # 兜底①（F-5 允许的唯一裸标题定位）：回读通道不可用（list_nodes 异常）→
            #   GUID/计数均不可得，退回旧行为，不新增失败面
            node = BPB.find_node_by_title(graph, raw_arg)
            if node is None:
                raise BridgeError(f"节点不存在: {raw_arg}", category="node_failure")
            node_guid = _pywrap_node_guid(node)
        elif len(matches) == 0:
            raise BridgeError(f"节点不存在: {raw_arg}", category="node_failure")
        elif len(matches) > 1:
            dups = [{"guid": (i.guid or "<GUID不可读>"),
                     "class": (i.node_class or "?"),
                     "pos": [i.pos_x, i.pos_y]} for i in matches]
            raise BridgeError(
                "节点标题歧义，拒绝执行: 「%s」在图中命中 %d 个同名节点 → "
                "按标题删除会误删且无法确定目标，请改用 GUID 精确指定。"
                "同名节点清单: %s" % (raw_arg, len(matches), dups),
                category="node_ambiguous",
                suggestion="改用 read_nodes 返回的 guid 字段作为 node_id 重试",
            )
        else:
            node_guid = matches[0].guid or ""
            node = None
            if node_guid:
                try:
                    node = BPB.get_node_by_guid(graph, node_guid)
                except Exception:
                    node = None
            if node is None:
                # 兜底②（F-5 允许的唯一裸标题定位）：命中恰 1 个但 GUID 不可读 →
                #   唯一命中下标题查找无歧义
                node = BPB.find_node_by_title(graph, raw_arg)
            if node is None:
                raise BridgeError(f"节点不存在: {raw_arg}", category="node_failure")
        matched_by = "title"

    # 回显/连线枚举统一用真实标题（GUID 入参 → 回读表反查；标题入参 → 原语义）
    node_id = (_pywrap_readback_title_of(graph, raw_arg)
               if _pywrap_is_guid_like(raw_arg)
               else (_pywrap_node_title(node) or raw_arg))

    # ── 枚举该节点全部连线 ──
    conns = []
    try:
        conns = BPB.get_node_connections_for_node(graph, node) or []
    except Exception:
        try:
            all_conns = BPB.get_node_connections(graph) or []
            conns = [c for c in all_conns
                     if (str(getattr(c, "from_node_title", "")) == node_id or
                         str(getattr(c, "to_node_title", "")) == node_id)]
        except Exception:
            conns = []
    broken_count = len(conns)

    # ── dry_run：仅检查引用 + 定位判定，不实际删除 ──
    if dry_run:
        return {
            "success": True,
            "data": {
                "dry_run": True, "deleted_node": node_id,
                "deleted_guid": node_guid or "",
                "matched_by": matched_by,
                "broken_links": broken_count,
                "removed_from_graph": False, "destroyed": False,
                "marked_dirty": False, "method": "python_api",
            },
            "detail": (f"干跑: {node_id} — 命中方式 {matched_by}"
                       f"（GUID {node_guid or '<不可读>'}），"
                       f"{broken_count} 条连线将被断开，未实际删除"),
        }

    # ── Step 1: 断开所有连线（跨图同属校验：只断同图连线）──
    broken = 0
    skipped_cross_graph = 0
    try:
        for pin in list(getattr(node, "pins", None) or []):
            if not getattr(pin, "linked_to", None):
                continue
            for other_pin in list(pin.linked_to):
                other_node = _pin_owner_node(other_pin)
                other_title = str(getattr(other_node, "node_title", "?"))
                same_graph = True
                try:
                    # F-5：跨图同属校验统一走 strict —— 对端同名歧义时**无法安全判定**
                    #   同属，保守跳过该条（不擅自断线），避免静默断错线。
                    _peer2, _peer2_err = _pywrap_find_peer_in_graph(graph, other_title)
                    if _peer2_err:
                        skipped_cross_graph += 1
                        continue
                    same_graph = _peer2 is not None
                except Exception:
                    same_graph = True
                if not same_graph:
                    skipped_cross_graph += 1
                    continue
                try:
                    pin.break_link_to(other_pin)
                    broken += 1
                except Exception:
                    pass
    except Exception:
        pass

    # ── Step 2: 从图中移除节点 ──
    # T-20260805 Bug3: 真实 UE 5.1 Python 绑定无 graph.remove_node /
    #   BlueprintEditorLibrary.remove_graph_node / node.destroy_node（均 AttributeError），
    #   K2Node/EdGraphNode 亦无 pins/remove/destroy 方法。
    #   → v0.8 优先 C++ RemoveNode API（UE 重启后生效，支持已连接节点）；
    #     降级 remove_unused_nodes（仅删未连接节点）。
    # T-20260911-DELNODE-GUID: 拿到真 GUID 时**优先走 GUID 精确删除原语**
    #   （`_pywrap_delete_node_by_guid`），彻底消除「标题重名」残余风险。
    removed = False
    destroyed = False
    try:
        if not hasattr(BPB, "remove_node"):
            # 目标节点有连线时 remove_unused_nodes 不会移除它 → 先诚实报错
            try:
                node_conns = BPB.get_node_connections_for_node(graph, node) or []
            except Exception:
                node_conns = []
            if node_conns:
                raise BridgeError(
                    f"节点移除失败: {node_id} — 节点仍有 {len(node_conns)} 条连线，"
                    f"当前插件无断线 API，请先手动断开",
                    category="node_remove",
                )
            unreal.BlueprintEditorLibrary.remove_unused_nodes(bp)
            removed = True
            # 验证目标节点已消失（F-5：统一 strict —— 有 GUID 走 GUID 校验，
            #   否则同名兄弟节点会让裸标题校验**假阳性**报「未移除」）
            _gone = True
            if node_guid:
                try:
                    _gone = BPB.get_node_by_guid(graph, node_guid) is None
                except Exception:
                    _gone = True
            else:
                _gone = BPB.find_node_by_title(graph, node_id) is None
            if not _gone:
                raise BridgeError(
                    f"节点移除失败: {node_id} — remove_unused_nodes 未移除该节点",
                    category="node_remove",
                )
        elif node_guid:
            # 首选：GUID 精确删除（同名节点场景唯一可靠）
            ok, why = _pywrap_delete_node_by_guid(graph, node_guid)
            if not ok:
                raise BridgeError(
                    f"节点移除失败: {node_id}（GUID {node_guid}）— {why}",
                    category="node_remove",
                )
            removed = True
        else:
            removed = BPB.remove_node(graph, node)
            if not removed:
                raise BridgeError(
                    f"节点移除失败: {node_id} — C++ RemoveNode 返回 False",
                    category="node_remove",
                )
    except BridgeError:
        raise
    except Exception as e3:
        raise BridgeError(
            f"节点移除失败: {node_id} — 移除失败: {e3}",
            category="node_remove",
        )

    # ── Step 3: 标记蓝图脏 ──
    marked_dirty = False
    try:
        bp.mark_package_dirty()
        marked_dirty = True
    except Exception:
        pass

    return {
        "success": True,
        "data": {
            "deleted_node": node_id,
            "deleted_guid": node_guid or "",
            "matched_by": matched_by,
            "broken_links": broken,
            "skipped_cross_graph_count": skipped_cross_graph,
            "removed_from_graph": removed,
            "destroyed": destroyed,
            "marked_dirty": marked_dirty,
            "method": "python_api",
        },
        "detail": (f"已删除 {node_id}（{matched_by} 命中，"
                   f"GUID {node_guid or '<不可读>'}，断开 {broken} 条连线）"),
    }


#  POST /build-batch — 批量创建蓝图（v0.6 P4：原子化，失败回滚）
# ══════════════════════════════════════════════════════════

class BuildBatchSpec(BaseModel):
    blueprint_name: str
    package_path: str = "/Game/Blueprints/"
    parent_class: str = "Actor"
    blueprint_type: str = "Actor"
    variables: List[Dict] = []
    components: List[Dict] = []
    interfaces: List[str] = []
    compile_after: bool = True
    events: List[Dict] = []
    functions: List[Dict] = []
    connections: List[Dict] = []

class BuildBatchRequest(BaseModel):
    blueprints: List[BuildBatchSpec]

@app.post("/build-batch")
def build_batch(req: BuildBatchRequest, request: Request = None):  # 🔧 B: async→sync def（T-20260806-UE5BRIDGE-CRASH-FIX 方案B·线程池隔离）
    """批量创建蓝图，全部成功或全部回滚。

    对每个 spec 依次调用 /build 的核心逻辑。
    任意一个失败 → 删除已创建的全部蓝图 → 返回错误。
    """
    global _request_count
    with _lock:
        _request_count += 1

    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")

    t0 = time.time()
    created_paths: List[str] = []
    results: List[Dict] = []

    for i, spec in enumerate(req.blueprints):
        full_path = f"{spec.package_path}{spec.blueprint_name}"
        obj_path = f"{full_path}.{spec.blueprint_name}"

        try:
            # ── D-8（T-20260917-D8-FUNCTIONS-CONTRACT）：functions 字段名先归一化 ──
            #   `/build-batch` 与 `/build` 是**同型两处透传**（L5580 gt_spec /
            #   L7278 本处）→ 同一归一化函数全覆盖；BridgeError 走下方 except
            #   → 整体回滚（不留半成品）。与 `/build` 保持完全一致语义。
            spec.functions, _fn_warnings = _normalize_build_functions(spec.functions)

            # ── 复用 /build 的核心创建逻辑 ──
            existing = _run_on_game_thread_sync(unreal.EditorAssetLibrary.does_asset_exist, full_path)
            if not existing:
                existing = _run_on_game_thread_sync(unreal.EditorAssetLibrary.does_asset_exist, obj_path)
            if existing:
                raise BridgeError(
                    f"蓝图已存在: {full_path}",
                    category="asset_create",
                    suggestion="请使用不同的蓝图名称或先删除已有蓝图"
                )

            if BRIDGE_AVAILABLE:
                bp = _safe_call(
                    bridge.create_actor_blueprint,
                    spec.package_path, spec.blueprint_name,
                    error_cat="asset_create", pitfall_id="P07"
                )
            else:
                factory = unreal.BlueprintFactory()
                factory.set_editor_property("parent_class",
                    unreal.load_class(None, f"/Script/Engine.{spec.parent_class}"))
                bp = _safe_call(
                    unreal.AssetToolsHelpers.get_asset_tools().create_asset,
                    spec.blueprint_name, spec.package_path.rstrip("/"),
                    None, factory,
                    error_cat="asset_create"
                )

            if not bp:
                raise BridgeError(f"创建蓝图失败: {spec.blueprint_name}", category="asset_create")

            created_paths.append(full_path)

            # 变量
            for var_def in spec.variables:
                var_name = var_def.get("name", "")
                var_type = var_def.get("type", "bool")
                if BRIDGE_AVAILABLE:
                    _safe_call(bridge.add_member_variable, bp, var_name, var_type,
                               error_cat="variable_add")
                else:
                    _safe_call(_add_variable_unreal, bp, var_name, var_type, var_def,
                               error_cat="variable_add")

            # 组件
            for comp_def in spec.components:
                _safe_call(_add_component_unreal, bp,
                           comp_def.get("name", ""), comp_def.get("component_class", "SceneComponent"),
                           comp_def.get("properties", {}), error_cat="component_add")

            # 接口
            for iface in spec.interfaces:
                _safe_call(_add_interface_unreal, bp, iface, error_cat="interface_add")

            # ── 事件/函数/连线/编译（GameThread 原子执行）──
            if spec.events or spec.functions or spec.connections or spec.compile_after:
                _spec_file = os.path.join(
                    os.environ.get("TEMP", "/tmp"),
                    f"ue5_batch_spec_{uuid.uuid4().hex[:8]}.json"
                )
                _result_file = os.path.join(
                    os.environ.get("TEMP", "/tmp"),
                    f"ue5_batch_result_{uuid.uuid4().hex[:8]}.json"
                )
                _gt_spec = {
                    "blueprint_path": full_path,
                    "events": spec.events,
                    "functions": spec.functions,
                    "connections": spec.connections,
                    "compile_after": spec.compile_after,
                }
                with open(_spec_file, "w", encoding="utf-8") as f:
                    json.dump(_gt_spec, f, ensure_ascii=False)

                # N01 修复（2026-07-27）：从内联字符串拼接改为模板文件加载
                with open(os.path.join(os.path.dirname(__file__), "game_thread_build.py.tmpl"), "r", encoding="utf-8") as _f:
                    _gt_code = _wrap_gt_scope(_f.read().replace("{SPEC_FILE}", repr(_spec_file)).replace("{RESULT_FILE}", repr(_result_file)))

                if BRIDGE_AVAILABLE and hasattr(bridge, 'execute_python_on_game_thread'):
                    # 🔧 E: 路径② trace_id 标记（T-20260806-UE5BRIDGE-CRASH-FIX 方案E）
                    _OP_LOG.info(f"[trace_id={getattr(request.state, 'trace_id', '?')}] PATH2 execute_python_on_game_thread(build_batch)")
                    _gt_exec_python(_gt_code)
                    _waited = 0.0
                    while not os.path.exists(_result_file) and _waited < 30.0:
                        time.sleep(0.1)
                        _waited += 0.1
                    if os.path.exists(_result_file):
                        with open(_result_file, 'r', encoding='utf-8') as f:
                            _gt_result = json.load(f)
                        os.remove(_result_file)
                        if not _gt_result.get("success"):
                            raise BridgeError(
                                f"蓝图 {spec.blueprint_name} 图形操作失败: {_gt_result.get('error', 'unknown')}",
                                category="graph_exec"
                            )
                try:
                    os.remove(_spec_file)
                except Exception:
                    pass

            results.append({"index": i, "blueprint_name": spec.blueprint_name,
                            "path": full_path, "success": True,
                            # D-8：functions 字段名归一化告警（逐 spec 回传）
                            "warnings": _fn_warnings})

        except Exception as e:
            # ── 回滚：删除所有已创建的蓝图 ──
            rollback_errors = []
            for path in created_paths:
                try:
                    # D-1（T-20260913-UE5BRIDGE-DELETE-COMPILE-FIX）：
                    #   旧实现丢弃 delete_asset 返回值 → 回滚静默失效。
                    #   改走级联删除 + 删后回读；失败 → 计入 rollback_errors。
                    _rb = _delete_asset_cascade(path)
                    if not _rb.get("success"):
                        rollback_errors.append(
                            f"{path}: {_rb.get('error')}")
                except Exception as rb_err:
                    rollback_errors.append(f"{path}: {rb_err}")

            return {
                "success": False,
                "error": f"第 {i} 个蓝图 ({spec.blueprint_name}) 创建失败: {e}",
                "failed_index": i,
                "created_count": len(created_paths),
                "rollback": {
                    "cleaned": len(created_paths) - len(rollback_errors),
                    "errors": rollback_errors,
                },
                "results": results,
                "duration_ms": (time.time() - t0) * 1000,
            }

    # ── 全部成功 ──
    # D-8（T-20260917-D8-FUNCTIONS-CONTRACT）：聚合逐 spec 的 functions 告警
    _all_warnings = []
    for _r in results:
        for _w in (_r.get("warnings") or []):
            _all_warnings.append("blueprints[%d] %s" % (_r["index"], _w))
    return {
        "success": True,
        "total": len(req.blueprints),
        "created_paths": created_paths,
        "warnings": _all_warnings,
        "duration_ms": (time.time() - t0) * 1000,
    }


# ══════════════════════════════════════════════════════════
#  生命周期管理 — 启动 / 关闭 / 优雅降级
# ══════════════════════════════════════════════════════════

# 🔧 T-20260806-ATEXIT-UEVERIFY Bug②：server 状态跨 reload 持久化
#   reload 重置模块顶层赋值 → _server_running/_uvicorn_server/_server_thread 丢失
#   → reload 后 stop() 走 `if not _server_running: return`，停不掉旧 server
#   → 旧 daemon=False server 线程残留（atexit join 挂死 + 端口占用）。
#   修复：与 _GT_WORKER_HANDLE 相同的 getattr 恢复模式（reload 不重建模块对象）。
_server_thread = getattr(sys.modules.get(__name__), '_server_thread', None)
_uvicorn_server = getattr(sys.modules.get(__name__), '_uvicorn_server', None)
_server_running = getattr(sys.modules.get(__name__), '_server_running', False)
_atexit_registered = getattr(sys.modules.get(__name__), '_atexit_registered', False)


# ══════════════════════════════════════════════════════════
#  v3.0 REST 别名：/pie/* /logs /editor/batch
#  （薄封装，实现全在 v3.0 白名单命令中；token 鉴权同其他端点）
# ══════════════════════════════════════════════════════════

class PieStartRequest(BaseModel):
    mode: str = "simulate"

class LogsRequest(BaseModel):
    lines: int = 200
    category: str = ""
    pattern: str = ""
    cursor: int = 0
    max_bytes: int = 524288
    log_file: str = ""

class EditorBatchRequest(BaseModel):
    steps: List[Dict] = []
    stop_on_error: bool = True

def _endpoint_result(fn, *args, **kwargs):
    """统一端点包装：BridgeError/异常 → 结构化失败（fail-loud，不裸 500）。

    v3.0：若被包装函数已返回带 `success` 的信封（新命令族惯例）→ 原样透传
    （只补 duration_ms），避免双重 data 嵌套；其余（旧式裸结果）→ 包 envelope。
    """
    t0 = time.time()
    try:
        data = fn(*args, **kwargs)
        if isinstance(data, dict) and "success" in data:
            out = dict(data)
            out.setdefault("duration_ms", round((time.time() - t0) * 1000, 1))
            return out
        ok = True
        if isinstance(data, dict) and data.get("_fail_loud") and not data.get("success"):
            ok = False
        return {"success": ok, "data": data,
                "duration_ms": round((time.time() - t0) * 1000, 1)}
    except BridgeError as e:
        return {"success": False, "error": str(e), "category": e.category,
                "suggestion": e.suggestion,
                "duration_ms": round((time.time() - t0) * 1000, 1)}
    except Exception as e:
        return {"success": False, "error": "%s: %s" % (type(e).__name__, e),
                "traceback": traceback.format_exc()[-400:],
                "duration_ms": round((time.time() - t0) * 1000, 1)}

@app.post("/pie/start")
def pie_start_ep(req: PieStartRequest, request: Request = None):
    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")
    return _endpoint_result(_cmd_pie_start, req.mode)

@app.post("/pie/stop")
def pie_stop_ep(request: Request = None):
    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")
    return _endpoint_result(_cmd_pie_stop)

@app.post("/pie/state")
def pie_state_ep(request: Request = None):
    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")
    return _endpoint_result(_cmd_pie_state)

@app.post("/pie/actors")
def pie_actors_ep(req: Dict = Body(default={}), request: Request = None):
    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")
    req = req or {}
    return _endpoint_result(_cmd_find_actors,
                            req.get("actor_class", ""), req.get("name_filter", ""),
                            "pie", req.get("limit", 100))

@app.post("/pie/get_property")
def pie_get_property_ep(req: Dict = Body(...), request: Request = None):
    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")
    if not req.get("actor_name"):
        return {"success": False, "error": "actor_name 必填"}
    return _endpoint_result(_cmd_get_actor_properties,
                            req["actor_name"], req.get("property_name", ""),
                            "pie", req.get("limit", 200))

@app.post("/pie/set_transform")
def pie_set_transform_ep(req: Dict = Body(...), request: Request = None):
    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")
    if not req.get("actor_name"):
        return {"success": False, "error": "actor_name 必填"}
    return _endpoint_result(_cmd_set_actor_transform,
                            req["actor_name"], req.get("location"), req.get("rotation"),
                            "pie")

@app.post("/logs")
def logs_ep(req: LogsRequest, request: Request = None):
    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")
    return _endpoint_result(_cmd_logs_read, req.lines, req.category, req.pattern,
                            req.cursor, req.max_bytes, req.log_file)

@app.post("/editor/batch")
def editor_batch_ep(req: EditorBatchRequest, request: Request = None):
    if not _verify_token(request):
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing X-Skill-Token")
    return _endpoint_result(_cmd_editor_batch, req.steps, req.stop_on_error)


# ══════════════════════════════════════════════════════════
#  v3.0 MCP 协议接入层（T-20260918-V3-MCP-FUSION）
#  ── 设计约束与理由 ──
#  · 零依赖手写：官方 mcp SDK 要求 Python ≥3.10，而 UE5.1 内嵌 Python 为 3.9
#    （实测 python39.dll）→ 必须手写最小实现（JSON-RPC 2.0 / Streamable HTTP）。
#  · 传输：POST /mcp 单响应 JSON（Streamable HTTP 规范允许服务端返回单个
#    application/json 响应）；通知类消息 → 202 空体。
#  · 鉴权：与其余端点同一 token（X-Skill-Token 或 Authorization: Bearer）。
#  · 工具面：27 个高频工具（verb_noun 命名，v3.3 起；24=T-20261024-UE5BRIDGE-COMPILE-DIAGNOSE 新增
#    ue5_compile_and_diagnose；26=T-20261024-UE5BRIDGE-GRAPH-APPLY 新增
#    ue5_search_blueprint_nodes / ue5_apply_blueprint_graph；27=T-20261024-UE5BRIDGE-SCREENSHOT
#    新增 ue5_screenshot）+
#    `ue5_execute_command` 白名单透传（描述含全量命令名），
#    避免把 78 条命令平铺撑爆 tools/list 的客户端 token 预算。
#  · 复用：需要端点级语义（/build /connect 等）的工具体通过 _TrustedRequest
#    垫片调用既有端点函数 —— MCP 层已完成等价鉴权，零逻辑复制。
# ══════════════════════════════════════════════════════════

_MCP_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
_MCP_SERVER_INFO = {"name": "ue5-automation-bridge", "version": "6.5.0"}


class _TrustedRequest:
    """内部可信请求垫片：让既有端点函数的 _verify_token / trace_id 读取通过。

    仅在 /mcp 已完成等价 token 校验后使用（同一进程、同一 token）。
    """

    class _Headers:
        def get(self, key, default=None):
            if str(key).lower() == "x-skill-token":
                return _BRIDGE_TOKEN
            return default

    class _State:
        trace_id = "mcp"

    def __init__(self):
        self.headers = _TrustedRequest._Headers()
        self.state = _TrustedRequest._State()


def _mcp_obj(props, required=None):
    return {"type": "object", "properties": props, "required": required or []}


#: 核心工具（默认只暴露这些 —— T-20261027-SLIM）
#: 依据：① 4 天真实调用日志里 31 个工具只用了 12 个；未用的 19 个占常驻
#: token 的 44%；② 跨领域实测（blender-mcp 作者在 14-15 个工具时重构、
#: 工具数增长使选择准确率 43%→14%、3 个 MCP server 吃掉 72% 上下文）。
#: 扩展工具不消失 —— `ue5_execute_command` 覆盖全部白名单命令，仍可调用；
#: 需要完整目录时设环境变量 UE5_MCP_ALL_TOOLS=1。
#: 网关档（v6.1 · v6.5 扩到 12）：只暴露少量入口，其余经
#: tool_search/execute_command 发现。默认不启用（能力可发现性优先）。
GATEWAY_TOOLS = (
    "ue5_check_health",             # 连通性自检（首次接触）
    "ue5_tool_search",              # 按需发现（含被隐藏的工具）
    "ue5_execute_command",          # 万能壳：覆盖全部白名单命令
    "ue5_read_blueprint",           # 改图前先读（L1/L2/full）
    "ue5_search_blueprint_nodes",   # 写图前查函数/事件/引脚签名
    "ue5_apply_blueprint_graph",    # 整图写入（写图主路径）
    "ue5_compile_and_diagnose",     # 编译 + 结构化诊断（修复循环的反馈侧）
    "ue5_screenshot",               # 视觉自检（返回图片内容块）
    "ue5_verify_behavior",          # 运行时属性断言
    "ue5_verify_scenario",          # 多步 PIE 场景验证（CI 级断言/报告）
    "ue5_task_begin",               # 任务上报起点
    "ue5_task_end",                 # 任务上报终点（自动保存 + 证据）
)

CORE_TOOLS = (
    "ue5_execute_command",          # 万能壳：覆盖全部 101 条白名单命令
    "ue5_tool_search",              # 按需发现命令/工具（v4.9 上下文治理）
    "ue5_verify_behavior",          # 行为验证（v6.1 专用入口）
    "ue5_search_blueprint_nodes",   # 查签名（写图前必用）
    "ue5_apply_blueprint_graph",    # 整图写入（写图主路径）
    "ue5_build_blueprint",          # 建资产
    "ue5_compile_and_diagnose",     # 编译 + 结构化诊断
    "ue5_compile_blueprint",        # 纯编译
    "ue5_screenshot",               # 视觉自检
    "ue5_connect_pins",             # 手术式连线
    "ue5_disconnect_pin",           # 手术式断线
    "ue5_save_asset",               # 保存（防"只在内存里"）
    # 以下 3 项经可达性核查后加回（T-20261027-SLIM-FIX）：
    #   delete_node 无白名单等价命令（apply_graph 是替换语义，删单节点是独立能力）
    #   run_batch 提供多步 steps 语义（虽然 editor_batch 命令存在，工具形态更友好）
    #   run_diagnostics 是自检入口（无白名单等价）
    "ue5_delete_node",
    "ue5_run_batch",
    "ue5_run_diagnostics",
    "ue5_task_begin",
    "ue5_task_step",
    "ue5_task_end",
)


# ══════════════════════════════════════════════════════════════════
# 工具/命令检索（T-20261028-TOOL-SEARCH · 上下文治理的加法路线）
# ══════════════════════════════════════════════════════════════════
# 目标：保留全部 34 个工具 + 101 条命令（能力不缩），但不必把命令名
# 塞进 execute_command 的描述里。模型用 ue5_tool_search{query} 按需发现。
# 目录构建是纯反射：命令名来自白名单，参数来自 lambda 签名，说明来自实现
# 函数的 docstring 首行 —— 不维护第二份清单（避免漂移）。

_COMMAND_TARGET_RE = re.compile(
    r"(_cmd_[A-Za-z0-9_]+|bridge_diag|read_blueprint|build_blueprint|"
    r"disconnect_pin|delete_node)\s*\(")


def _command_catalog() -> List[dict]:
    """白名单命令 → [{name, args, desc}]（反射；失败字段留空，绝不编造）。"""
    out = []
    for name in sorted(_COMMAND_WHITELIST):
        fn = _COMMAND_WHITELIST[name]
        args: List[str] = []
        try:
            args = [p for p in inspect.signature(fn).parameters
                    if p not in ("kw", "kwargs")]
        except Exception:
            args = []
        desc = ""
        try:
            src = inspect.getsource(fn)
            m = _COMMAND_TARGET_RE.search(src)
            if m:
                target = globals().get(m.group(1))
                if target is not None:
                    desc = (inspect.getdoc(target) or "").splitlines()[0][:140]
        except Exception:
            desc = ""
        out.append({"name": name, "args": args, "desc": desc})
    return out


def _catalog_match(entry: dict, tokens: List[str]) -> bool:
    hay = (entry.get("name", "") + " " + entry.get("desc", "")).lower()
    return all(t in hay for t in tokens)


def _cmd_tool_search(query: str = "", limit: int = 8,
                     include_tools: bool = True) -> dict:
    """按关键词检索白名单命令与 MCP 工具（按需发现，替代把清单塞进描述）。

    query 支持英文命令名/中文说明的任意词组合（全部命中才返回）。
    """
    q = str(query or "").strip().lower()
    if len(q) > 200:
        raise BridgeError("query 过长（上限 200 字符）", category="param_type")
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 8
    tokens = [t for t in re.split(r"[\s,，]+", q) if t]
    cmds = [c for c in _command_catalog() if not tokens or _catalog_match(c, tokens)]
    # 精确命中排前（命令名完全等于 query 或以其开头）
    cmds.sort(key=lambda c: (0 if c["name"] == q else 1 if c["name"].startswith(q) else 2,
                             c["name"]))
    tools_out = []
    if include_tools:
        for t in _mcp_tool_defs():
            nm = t.get("name", "")
            ds = t.get("description", "")
            hay = (nm + " " + ds).lower()
            if not tokens or all(tok in hay for tok in tokens):
                tools_out.append({"name": nm, "desc": ds.split("|")[0].strip()[:140]})
            if len(tools_out) >= 5:
                break
    return {
        "success": True,
        "query": query,
        "count": len(cmds),
        "commands": cmds[:limit],
        "tools": tools_out,
        "hint": ("调用方式：execute_command{command:<name>, params:{...}}；"
                 "args 即 params 的键。命令名未命中时换更短的关键词（如 material / umg / pie）。"),
    }


def _mcp_tool_defs_core() -> list:
    """tools/list 清单：**默认全量 34 项**（v4.8 起）。

    历史教训（T-20261028-TOOL-RESTORE）：v4.3 曾按「4 天调用日志里 31 个只用了
    12 个」把默认目录收窄到 16 个 —— 这是统计错误：样本小、场景偏，只能说明
    未测到，不能说明无价值。现改为默认全量，按需收窄：

      · 默认                   → 全量 34（能力完整、可发现性优先）
      · UE5_MCP_PROFILE=core   → 18 个核心工具（小上下文可选档）
      · UE5_MCP_PROFILE=gateway→ 12 个入口（极致档）
      · 旧 UE5_MCP_CORE_TOOLS=1 / UE5_MCP_GATEWAY=1 仍兼容
    """
    all_defs = _mcp_tool_defs()
    env = lambda k: str(os.environ.get(k, "0")).strip().lower() in ("1", "true", "yes")
    # v6.5：显式 profile（gateway / core / full）；兼容旧的两个布尔开关。
    profile = str(os.environ.get("UE5_MCP_PROFILE", "")).strip().lower()
    if profile == "gateway" or (not profile and env("UE5_MCP_GATEWAY")):
        gw = [t for t in all_defs if t.get("name") in GATEWAY_TOOLS]
        return gw or all_defs
    if profile == "core" or (not profile and env("UE5_MCP_CORE_TOOLS")):
        core = [t for t in all_defs if t.get("name") in CORE_TOOLS]
        return core or all_defs
    return all_defs


def _mcp_tool_defs():
    """MCP tools/list 清单（请求时构造 —— ue5_execute_command 描述含动态白名单）。

    v3.3 命名约定：全部 `ue5_<verb>_<noun>`，动词族固定为
      check_ / run_ / list_ / read_ / build_ / compile_ / connect_ /
      disconnect_ / delete_ / save_ / start_ / stop_ / set_ / execute_ /
      search_ / apply_（后两者 = T-20261024-UE5BRIDGE-GRAPH-APPLY 整图族）
    描述为「英文主 + 中文注」，含 Use-when 指引与返回形态；参数 schema 带逐项说明
    （面向 AI 客户端的工具选择质量，见 CHANGELOG v3.3）。
    """
    def P(t, d):
        return {"type": t, "description": d}

    BP = P("string", "Blueprint asset path, e.g. /Game/BP_Test | 蓝图资产路径")
    GRAPH = P("string", "Graph name; omit to search all graphs | 图名，缺省=全图搜索")
    NODE = P("string", "Node title or 32-hex GUID; GUID is recommended when titles "
                       "are duplicated | 节点标题或 32 位 GUID，标题歧义时必须 GUID")
    PIN = P("string", "Pin name on the node, e.g. 'then', 'execute', 'ReturnValue' "
                      "| 引脚名")
    ASSET = P("string", "Asset path, e.g. /Game/BP_Test | 资产路径")
    V3 = {"type": "array", "items": {"type": "number"}, "description": "[x, y, z]"}
    TOOLS = [
        {"name": "ue5_check_health",
         "description":
             "Check bridge/editor health: engine version, project name, loaded bridge "
             "API list. Call this first to confirm the editor is reachable before any "
             "write operation. Returns {engine_version, project, bridge_apis[]}. "
             "| 健康检查：引擎版本、项目名、bridge API 清单；任何写操作前先调用。",
         "inputSchema": _mcp_obj({})},
        {"name": "ue5_run_diagnostics",
         "description":
             "Full bridge diagnostics: complete C++ API method matrix and any missing "
             "critical APIs. Use when a tool fails with an API-missing error, or after "
             "upgrading/replacing the bridge DLL. | Bridge 诊断：全部 C++ API 方法矩阵 "
             "+ 关键 API 缺失项。",
         "inputSchema": _mcp_obj({})},
        {"name": "ue5_list_assets",
         "description":
             "List asset paths under a content folder (default /Game/). Use to discover "
             "what exists before reading or editing. recursive=true walks subfolders. "
             "| 列出 /Game 资产路径（recursive 控制是否递归）。",
         "inputSchema": _mcp_obj({
             "path": P("string", "Content folder to list, e.g. /Game/ | 要列出的目录"),
             "recursive": P("boolean", "Walk subfolders (default true) | 是否递归")},
             ["path"])},
        {"name": "ue5_read_blueprint",
         "description":
             "Read a Blueprint asset: level L1 = class info + variables + CDO, "
             "L2 = node graph + connections, full = both. Read-only and safe to call "
             "any time. Returns structured JSON (Markdown/Mermaid reports available via "
             "the CLI reader). | 读取蓝图（L1 类信息+变量+CDO / L2 节点+连线 / full）。",
         "inputSchema": _mcp_obj({
             "blueprint_path": BP,
             "level": P("string", "L1 | L2 | full (default L1) | 读取层级"),
             "include_cdo": P("boolean", "Include class-default-object values "
                                         "| 是否包含 CDO"),
             "include_connections": P("boolean", "Include pin connections "
                                                "| 是否包含连线"),
             "fields": P("array", "Keep only these node/connection fields, e.g. "
                                  "['title','guid','pos'] | 只返回指定字段（省 token）")},
             ["blueprint_path"])},
        {"name": "ue5_read_nodes",
         "description":
             "Read Blueprint nodes: titles, positions, GUIDs and pin defaults. Omit "
             "graph_name to search all graphs. Use before connecting pins so you have "
             "the GUIDs. | 读取蓝图节点（不传 graph_name = 全图搜索）。",
         "inputSchema": _mcp_obj({"bp_path": BP, "graph_name": GRAPH,
             "fields": P("array", "Keep only these fields | 只返回指定字段")},
             ["bp_path"])},
        {"name": "ue5_read_connections",
         "description":
             "Read the Blueprint's connection topology (which pin connects to which "
             "node/pin). Use to verify wiring before and after edits. | 读取蓝图连线拓扑。",
         "inputSchema": _mcp_obj({"bp_path": BP, "graph_name": GRAPH,
             "fields": P("array", "Keep only these fields | 只返回指定字段")},
             ["bp_path"])},
        {"name": "ue5_build_blueprint",
         "description":
             "Create a Blueprint from scratch in one atomic call: asset + variables + "
             "components + interfaces + events + function nodes + connections + "
             "optional compile. If any step fails, assets created by this call are "
             "rolled back. spec fields: blueprint_name (or name), package_path, "
             "parent_class, variables[{name,type,default_value,category}], "
             "components[{name,component_class,properties}], interfaces[str], "
             "events[{event_name|name,pos}], functions[{function_name|name, "
             "target_class, function_path, defaults:{pin:val}, pos}], "
             "connections[{src_node,src_pin,dst_node,dst_pin}], compile_after. "
             "Node references accept 32-hex GUIDs (recommended) or titles. "
             "| 创建蓝图（原子化：失败回滚已建资产）。",
         "inputSchema": _mcp_obj({
             "spec": P("object", "Blueprint build spec (see description for fields) "
                                 "| 蓝图构建规格，字段见描述"),
             "session_id": P("string", "Optional session/owner for asset lock checks "
                                      "| 可选：会话标识（多 Agent 资产锁）"),
             "idempotency_key": P("string", "Optional idempotency key; a repeated key "
                                            "within 10 min replays the prior success "
                                            "| 可选：幂等键（10 分钟内重放上次成功结果）")},
             ["spec"])},
        {"name": "ue5_build_batch",
         "description":
             "Create multiple Blueprints in one atomic batch: if any blueprint fails, "
             "all assets created by this batch are rolled back. Prefer ue5_build_blueprint "
             "unless multi-asset atomicity is actually needed. "
             "| 批量创建蓝图（原子化：任一失败回滚本批已建资产）。",
         "inputSchema": _mcp_obj({
             "blueprints": P("array", "Array of build specs, same schema as "
                                      "ue5_build_blueprint.spec | 构建规格数组")},
             ["blueprints"])},
        {"name": "ue5_compile_blueprint",
         "description":
             "Compile a Blueprint, read back the compile status and scan the log for "
             "error patterns. The real compile result is reported: a failed compile "
             "returns failure with log evidence, never a fake success. "
             "| 编译蓝图 + 回读编译状态 + 日志错误模式扫描。",
         "inputSchema": _mcp_obj({"bp_path": BP}, ["bp_path"])},
        {"name": "ue5_compile_and_diagnose",
         "description":
             "Compile a Blueprint AND return structured compiler errors: each error "
             "carries asset/function/node_name plus node_guid (the positioning key "
             "for incremental edit tools), pin hint when parseable, and a rule-map "
             "pitfall_ref/fix_hint. Fields that cannot be parsed are null — never "
             "fabricated. Use this instead of ue5_compile_blueprint when a compile "
             "fails and you need to locate and fix the offending nodes. "
             "fix_hint is advisory only, never a substitute for your own analysis. "
             "| 编译蓝图 + 结构化错误诊断（node_guid 定位 + 规则库映射）；"
             "编译失败需定位修复时用它替代 ue5_compile_blueprint。"
             "fix_hint 仅供参考，不代替模型判断。",
         "inputSchema": _mcp_obj({"bp_path": BP}, ["bp_path"])},
        {"name": "ue5_search_blueprint_nodes",
         "description":
             "Search callable nodes in a Blueprint (events / function candidates / "
             "flow control / variables) WITH full pin signatures — read-only. Use "
             "this BEFORE ue5_apply_blueprint_graph to get accurate targets and "
             "pin signatures for the GraphSpec. Engine-library candidates carry "
             "names + owning class; their pins are honestly empty (pure-Python "
             "channel cannot read UFunction signatures) and say so. "
             "| 只读搜索蓝图可调用节点（事件/函数/流程控制/变量候选）+ 完整引脚签名。"
             "写 ue5_apply_blueprint_graph 的 GraphSpec 前先调它拿准确签名；"
             "引擎库候选如实不带引脚（纯 Python 通道读不到 UFunction 签名）。",
         "inputSchema": _mcp_obj({
             "bp_path": BP,
             "query": P("string", "Substring filter, case-insensitive; empty = "
                                  "browse all | 子串过滤（大小写不敏感），空 = 全量浏览"),
             "limit": P("integer", "Max results (default 20, cap 50) "
                                   "| 返回上限（默认 20，封顶 50）")},
             ["bp_path"])},
        {"name": "ue5_apply_blueprint_graph",
         "description":
             "Declarative whole-graph write: one atomic transaction builds/wires "
             "the GraphSpec, compiles outside it, verifies by read-back, rolls "
             "back on failure. Main path for graph logic; build the spec from "
             "ue5_search_blueprint_nodes first. Node-level tools "
             "(ue5_connect_pins / ue5_delete_node) only for surgical fixes. "
             "| 整图写入主路径：原子事务 + 读回 verified + 失败回滚；先 search 再 apply。",
         "inputSchema": _mcp_obj({
             "bp_path": BP,
             "graph_spec": P("object", "GraphSpec: {graph, nodes:[{id, kind, "
                                       "target, class?, pos?}], "
                                       "edges:[{from:\"id.pin\", to:\"id.pin\"}], "
                                       "pin_defaults:{\"id.pin\": value}} — "
                                       "see SKILL.md §3.7 | 图规格（节点/边/默认值，"
                                       "见 SKILL.md §3.7）"),
             "replace": P("boolean", "Replace existing graph content (required "
                                     "true when graph is non-empty) | 替换语义开关"
                                     "（图非空时必须显式 true）"),
             "session_id": P("string", "Optional session/owner for asset lock checks "
                                      "| 可选：会话标识（多 Agent 资产锁）"),
             "idempotency_key": P("string", "Optional idempotency key; a repeated key "
                                            "within 10 min replays the prior success "
                                            "| 可选：幂等键（10 分钟内重放上次成功结果）")},
             ["bp_path", "graph_spec"])},
        {"name": "ue5_connect_pins",
         "description":
             "Connect pins with triple topology verification (existence, type "
             "compatibility, read-back). Node references accept titles or 32-hex GUIDs; "
             "ambiguous titles are rejected (fail-loud) — use GUIDs when titles repeat. "
             "| 引脚连线（三重拓扑验证；标题歧义 fail-loud 拒绝）。",
         "inputSchema": _mcp_obj({
             "blueprint_path": BP,
             "connections": P("array", "List of {src_node, src_pin, dst_node, dst_pin} "
                                       "| 连线列表")},
             ["blueprint_path", "connections"])},
        {"name": "ue5_disconnect_pin",
         "description":
             "Disconnect every connection attached to one pin of a node, verified "
             "against the live topology. | 断开节点某引脚的全部连线（按实时拓扑验证）。",
         "inputSchema": _mcp_obj(
             {"blueprint_path": BP, "node_id": NODE, "pin_name": PIN},
             ["blueprint_path", "node_id", "pin_name"])},
        {"name": "ue5_delete_node",
         "description":
             "Delete a Blueprint node. dry_run=true only checks references without "
             "deleting — use it first for nodes that may be referenced elsewhere. "
             "| 删除节点（dry_run 仅检查引用，不删除）。",
         "inputSchema": _mcp_obj(
             {"blueprint_path": BP, "node_id": NODE,
              "dry_run": P("boolean", "Check references only, do not delete "
                                      "| 仅检查引用")},
             ["blueprint_path", "node_id"])},
        {"name": "ue5_save_asset",
         "description":
             "Save an asset to disk; only_if_is_dirty=true skips clean packages. "
             "Saves are scoped to the asset's own package — this tool never triggers a "
             "whole-project save. | 保存资产落盘（only_if_is_dirty=true 仅脏包写盘；"
             "作用域=该资产包，绝不全工程保存）。",
         "inputSchema": _mcp_obj({
             "asset_path": ASSET,
             "only_if_is_dirty": P("boolean", "Skip clean packages (default false) "
                                              "| 仅脏包写盘")},
             ["asset_path"])},
        {"name": "ue5_delete_asset",
         "description":
             "Delete an asset with reference checks and read-back verification. Save "
             "scope = referencing packages only. | 级联删除资产（删后回读验证；"
             "保存作用域=引用者包）。",
         "inputSchema": _mcp_obj({
             "asset_path": ASSET,
             "max_attempts": P("integer", "Delete retry attempts (default 3) "
                                          "| 删除重试次数")},
             ["asset_path"])},
        {"name": "ue5_start_pie",
         "description":
             "Start PIE (play/simulate) asynchronously. Poll with ue5_read_pie_state "
             "until is_playing=true. Use to verify that logic actually RUNS, not just "
             "that it compiles. | 启动 PIE/Simulate（异步；用 ue5_read_pie_state 轮询确认）。",
         "inputSchema": _mcp_obj({
             "mode": P("string", "'simulate' (default) | 'play' | 启动模式")})},
        {"name": "ue5_stop_pie",
         "description":
             "Stop the running PIE/Simulate session. | 结束 PIE/Simulate 会话。",
         "inputSchema": _mcp_obj({})},
        {"name": "ue5_read_pie_state",
         "description":
             "Read PIE state: is_playing, world name, paused, game time. Poll this "
             "after ue5_start_pie before reading runtime actors/properties. "
             "| PIE 状态：is_playing / 世界名 / 暂停 / 游戏时间。",
         "inputSchema": _mcp_obj({})},
        {"name": "ue5_list_pie_actors",
         "description":
             "Enumerate live Actors in the PIE world (runtime objects; their paths "
             "contain UEDPIE_0_). Filter by actor_class and/or name_filter substring; "
             "limit caps the result count. | 枚举 PIE 世界 Actor（运行时对象）。",
         "inputSchema": _mcp_obj({
             "actor_class": P("string", "Filter by class name, e.g. StaticMeshActor "
                                        "| 按类名过滤"),
             "name_filter": P("string", "Substring match on actor name | 名称子串过滤"),
             "limit": P("integer", "Max actors returned (default 100) | 返回上限")})},
        {"name": "ue5_read_pie_property",
         "description":
             "Read a runtime Actor property in PIE; omit property_name to list "
             "candidate property names first. Use this to prove game logic ran (e.g. "
             "health changed after a pickup). | 读 PIE 中 Actor 属性"
             "（property_name 空 = 列候选属性名）。",
         "inputSchema": _mcp_obj({
             "actor_name": P("string", "Runtime actor name/label in PIE "
                                       "| PIE 中 Actor 名"),
             "property_name": P("string", "Property to read; empty = list candidates "
                                          "| 属性名，空=列候选")},
             ["actor_name"])},
        {"name": "ue5_set_pie_transform",
         "description":
             "Teleport a PIE Actor at runtime (location and/or rotation, at least one), "
             "with read-back verification of the new transform. "
             "| 运行时传送 PIE Actor（location/rotation 至少一个；带回读验证）。",
         "inputSchema": _mcp_obj(
             {"actor_name": P("string", "Runtime actor name/label in PIE "
                                        "| PIE 中 Actor 名"),
              "location": V3,
              "rotation": {"type": "array", "items": {"type": "number"},
                           "description": "[pitch, yaw, roll]"}},
             ["actor_name"])},
        {"name": "ue5_screenshot",
         "description":
             "Screenshot for visual self-check (edit -> screenshot -> inspect -> fix). "
             "source: editor_viewport | agent_camera (camera {pitch,yaw,roll} required) "
             "| pie (fails loudly if PIE not running). Returns an image block + "
             "saved_path / camera_used provenance; PNG also saved for the dashboard. "
             "| 视觉自检截图；camera_used 可溯源，PIE 未运行 fail-loud。",
         "inputSchema": _mcp_obj({
             "source": P("string", "'editor_viewport' | 'agent_camera' | 'pie' "
                                   "(default editor_viewport) | 截图来源"),
             "camera": {"type": "object",
                        "description": "agent_camera rig {pitch, yaw, roll}; "
                                       "REQUIRED for source=agent_camera | "
                                       "agent_camera 的机位角（该来源必填）"},
             "annotate": P("boolean", "Draw visible actor name labels (default true; "
                                      "needs Pillow) | 是否叠加 actor 名标注"),
             "max_dimension": P("integer", "Long-edge cap of the returned image "
                                           "(default 768) | 返回图像长边上限"),
             "out_name": P("string", "Optional relative filename inside the "
                                     "screenshot folder, e.g. 'shot_a.png' | "
                                     "约定目录内的相对文件名（缺省自动命名）")},
             ["source"])},
        {"name": "ue5_task_begin",
         "description":
             "Declare a task for the dashboard: goal, asset, understanding, "
             "approach, steps. One active task at a time; close with ue5_task_end. "
             "| 声明任务（意图/计划/步骤），面板据此展示脉络。",
         "inputSchema": _mcp_obj({
             "goal": P("string", "One-line task goal | 一句话任务意图"),
             "user_request": P("string", "The user's original words (verbatim) "
                                        "| 用户的原始需求原话（尽量逐字，用于"
                                        "「原话 vs 我的理解」对照）"),
             "asset": P("string", "Primary asset path | 主目标资产路径"),
             "understanding": P("string", "Blueprint intent analysis | 蓝图意图分析"),
             "approach": P("string", "How it will be implemented in UE | 实现思路"),
             "steps": {"type": "array",
                       "description": "Plan steps: [{name, hint}] or [\"...\"] "
                                      "| 计划步骤"}},
             ["goal"])},
        {"name": "ue5_task_step",
         "description":
             "Report task progress against the declared plan: step_id (from "
             "ue5_task_begin steps) + status running|done|failed|skipped, or "
             "a free note when between steps. evidence may carry screenshot "
             "paths / error counts for the dashboard. "
             "| 上报进度：step_id 定位计划步骤 + status；步骤间可发 note 自由"
             "注记；evidence 可带截图路径/错误数作为证据。",
         "inputSchema": _mcp_obj({
             "step_id": P("integer", "Plan step id | 计划步骤号"),
             "status": P("string", "running | done | failed | skipped"),
             "note": P("string", "Progress note | 进度注记"),
             "evidence": {"type": "object",
                          "description": "{screenshots:[paths], errors:n, ...}"}})},
        {"name": "ue5_task_end",
         "description":
             "Close the active task with a retrospective: status "
             "success|failed|aborted, summary (what was implemented), lessons "
             "(practical takeaways - what to avoid over-complicating next "
             "time, better approaches found). Dashboard shows duration, "
             "failed steps, and the retrospective. "
             "| 收尾复盘：status 必填；summary 实现了什么；lessons 实操经验"
             "（下次避免简单问题复杂化/新方法）。面板自动算总耗时与失败步骤。",
         "inputSchema": _mcp_obj({
             "status": P("string", "success | failed | aborted"),
             "summary": P("string", "What was implemented | 实现了什么"),
             "lessons": P("string", "Practical takeaways | 实操经验"),
             "deliverables": {"type": "array",
                              "description": "Deliverable list [{kind, path, action, "
                                             "note}] — files/assets created or modified "
                                             "| 交付物清单（新建/修改的文件与资产）"},
             "how_to_verify": P("string", "How the user verifies the work (steps) "
                                         "| 用户如何验证（操作步骤）")},
             ["status"])},
        {"name": "ue5_task_list",
         "description":
             "List journalized tasks (active + history) with their plans, "
             "progress and retrospectives. "
             "| 列出任务日志（进行中 + 历史），含计划/进度/复盘。",
         "inputSchema": _mcp_obj({})},
        {"name": "ue5_read_logs",
         "description":
             "Read the UE log: cursor=0 returns the tail (last `lines`); cursor>0 "
             "returns only new lines after that cursor (incremental polling). Filter "
             "by category/pattern. Use for self-service cross-checks of any operation. "
             "| 读 UE 日志（cursor=0 取尾部；cursor>0 增量取新增；category/pattern 过滤）。",
         "inputSchema": _mcp_obj({
             "lines": P("integer", "Tail size when cursor=0 (default 200) "
                                   "| 尾部行数"),
             "category": P("string", "Log category filter, e.g. LogBlueprint "
                                     "| 日志类别过滤"),
             "pattern": P("string", "Substring filter on the line | 行内容子串过滤"),
             "cursor": P("integer", "Incremental cursor from a previous call "
                                    "| 上次返回的增量游标")})},
        {"name": "ue5_run_batch",
         "description":
             "Execute a whitelisted-command batch in one request (multi-step task in a "
             "single round trip); stops on the first failing step by default. For a "
             "single operation prefer the dedicated tool — this is for sequences. "
             "| 白名单命令批量执行（单次请求多步；任一步失败默认中止）。",
         "inputSchema": _mcp_obj({
             "steps": P("array", "List of {command, params} steps | 步骤列表 "
                                 "{command, params}"),
             "stop_on_error": P("boolean", "Abort on first failure (default true) "
                                           "| 失败即中止")},
             ["steps"])},
        {"name": "ue5_execute_command",
         "description": ("Escape hatch: run any whitelisted bridge command by name — "
                         "the full "
                         + str(len(_COMMAND_WHITELIST))
                         + "-command surface, including capabilities without a "
                         "dedicated tool (materials, UMG widgets, and other advanced "
                         "operations). Prefer the dedicated tool whenever one exists "
                         "for the operation; this generic entry point is the last "
                         "resort and its arguments are less validated. "
                         "Use ue5_tool_search{query} to find exact command names "
                         "and their params. "
                         + str(len(_COMMAND_WHITELIST))
                         + " commands cover materials/UMG/tasks/undo/PIE/logs/batch. "
                           "Unknown command → fail-loud拒绝。"
                           "| 白名单透传；命令名用 ue5_tool_search 查。"),
         "inputSchema": _mcp_obj({
             "command": P("string", "Whitelisted command name (see description list) "
                                    "| 白名单命令名（见描述中的全量清单）"),
             "params": P("object", "Command arguments object | 该命令的入参对象"),
             "session_id": P("string", "Optional session/owner for asset lock checks "
                                      "| 可选：会话标识（写命令做协作锁检查）"),
             "idempotency_key": P("string", "Optional idempotency key; a repeated key "
                                            "within 10 min replays the prior success "
                                            "| 可选：幂等键（10 分钟内重放上次成功结果）")},
             ["command"])},
        {"name": "ue5_verify_scenario",
         "description": ("Run a scripted acceptance scenario in PIE and emit a JUnit "
                         "report: wait / assert property / screenshot / log_contains / "
                         "command steps, with automatic PIE start/stop and a total "
                         "timeout. Destructive commands are rejected unless "
                         "allow_destructive=true. Returns verified + per-step evidence "
                         "+ junit_path (CI-consumable). "
                         "| 场景化验收：PIE 内逐步等待/断言/截图/日志/命令，自动起停 PIE，"
                         "总超时保护；高危命令默认拒绝。verified 是唯一判据。"),
         "inputSchema": _mcp_obj({
             "name": P("string", "Scenario name | 场景名"),
             "steps": P("array", "Step list: wait/assert/screenshot/log_contains/"
                                 "command/pie_start/pie_stop | 步骤数组"),
             "start_pie": P("boolean", "Start PIE automatically (default true)"),
             "pie_mode": P("string", "simulate (default on 5.1) | pie"),
             "stop_pie": P("boolean", "Stop PIE at the end (default true)"),
             "junit_path": P("string", "Optional JUnit XML output path"),
             "stop_on_failure": P("boolean", "Stop at first failing step (default true)"),
             "timeout_s": P("number", "Total scenario timeout seconds "
                                      "(default 300, cap 1800) | 场景总超时"),
             "allow_destructive": P("boolean", "Allow destructive command steps "
                                              "(default false) | 放行高危命令")},
             ["name", "steps"])},
        {"name": "ue5_verify_behavior",
         "description": ("Assert runtime behavior: read a live property and compare "
                         "with the expected value (exact / tolerance / contains), "
                         "optionally capture a PIE screenshot as evidence. Requires "
                         "PIE/Simulate running; mismatch is fail-loud. "
                         "| 行为验证：读运行时属性与期望比对（支持数值容差/子串），"
                         "可附截图证据；PIE 未运行会 fail-loud。"),
         "inputSchema": _mcp_obj({
             "actor_name": P("string", "Actor name in the running world | Actor 名"),
             "property_name": P("string", "Reflectable property | 可读属性名"),
             "expected": P("string", "Expected value | 期望值"),
             "world": P("string", "pie (default) | editor"),
             "match": P("string", "exact (default) | contains"),
             "tolerance": P("number", "Numeric tolerance for exact | 数值容差"),
             "capture": P("boolean", "Capture screenshot evidence (default true)"),
             "out_path": P("string", "Screenshot relative path (optional)")},
             ["actor_name", "property_name", "expected"])},
        {"name": "ue5_tool_search",
         "description": ("Search the " + str(len(_COMMAND_WHITELIST)) + " whitelisted commands and the MCP tools by "
                         "keyword; returns command names, params and one-line "
                         "descriptions. Use this before execute_command to discover "
                         "capabilities without a dedicated tool. "
                         "| 按关键词检索白名单命令/MCP 工具（参数与一句话说明）；"
                         "调用 execute_command 前先用它查命令名。"),
         "inputSchema": _mcp_obj({
             "query": P("string", "Keyword(s), e.g. 'material', 'umg', 'pie', "
                                  "'undo', '素材' | 关键词（英文命令名或中文说明）"),
             "limit": P("integer", "Max commands (default 8, cap 20) | 返回命令数上限"),
             "include_tools": P("boolean", "Also search MCP tools (default true) "
                                           "| 同时检索 MCP 工具")},
             ["query"])},
    ]
    return TOOLS


def _mcp_impl_command(args):
    cmd = str(args.get("command") or "").strip()
    params = args.get("params") or {}
    if cmd not in _COMMAND_WHITELIST:
        raise BridgeError("Unknown command: %s" % cmd, category="param_type",
                          suggestion="可用命令：%s" % ", ".join(sorted(_COMMAND_WHITELIST.keys())))
    _guard_call("ue5_execute_command", {"command": cmd, "params": params,
                                       "confirm": params.get("confirm")})
    # v6.5 多 Agent：顶层 session_id/idempotency_key 透传到 dispatch 元数据层
    merged = dict(params)
    for k in ("session_id", "idempotency_key"):
        if args.get(k):
            merged[k] = args[k]
    r = _dispatch_with_tx(cmd, _COMMAND_WHITELIST[cmd], **merged)
    if isinstance(r, dict) and r.get("_fail_loud") and not r.get("success"):
        return {"success": False, "command": cmd, "error": str(r.get("error", "")),
                "result": r}
    return {"success": True, "command": cmd, "result": r}


def _mcp_impl_build(args):
    spec = args.get("spec")
    if not isinstance(spec, dict):
        raise BridgeError("spec 必须为对象", category="param_type")
    spec = dict(spec)
    # 与 ue5_runner.build 同口径的字段兼容（name→blueprint_name / auto_compile→compile_after）
    if "name" in spec and "blueprint_name" not in spec:
        spec["blueprint_name"] = spec.pop("name")
    if "package_path" in spec:
        spec["package_path"] = str(spec["package_path"]).rstrip("/") + "/"
    if "auto_compile" in spec and "compile_after" not in spec:
        spec["compile_after"] = spec.pop("auto_compile")
    # v6.5：build 也走 dispatch 元数据层（会话锁/幂等/事务）
    def _build(**kw):
        return build_blueprint(BuildRequest(**kw), _TrustedRequest())

    return _dispatch_args_with_tx(
        "build_blueprint", _build, **spec,
        session_id=args.get("session_id", ""),
        idempotency_key=args.get("idempotency_key", ""))


def _mcp_impl_apply(args):
    """ue5_apply_blueprint_graph：走 dispatch 元数据层（会话锁/幂等/事务）。"""
    bp_path = str(args.get("bp_path") or "")
    graph_spec = args.get("graph_spec")
    replace = bool(args.get("replace", False))
    return _dispatch_args_with_tx(
        "apply_graph",
        lambda **kw: _cmd_apply_graph(kw.get("bp_path", ""),
                                      kw.get("graph_spec"), kw.get("replace", False)),
        bp_path=bp_path, graph_spec=graph_spec, replace=replace,
        session_id=args.get("session_id", ""),
        idempotency_key=args.get("idempotency_key", ""))


def _mcp_impl_build_batch(args):
    bps = args.get("blueprints")
    if not isinstance(bps, list) or not bps:
        raise BridgeError("blueprints 必须为非空数组", category="param_type")
    return build_batch(BuildBatchRequest(blueprints=bps), _TrustedRequest())


def _mcp_impl_screenshot(args):
    """ue5_screenshot —— 截图反馈闭环的 MCP 门面（T-20261024-UE5BRIDGE-SCREENSHOT）。

    ⚠️ 调用线程契约：本实现由 _mcp_handle_one 特判为【HTTP 线程直呼】（绝不被
    _gt_run_handler 整体包进 GT worker）—— pending 截图文件由帧推进产生，
    GT 上轮询 = 阻塞帧推进 = 自死锁。GT 触达点只有白名单命令（经
    _capture_cmd_dispatch → _gt_run_handler，嵌套安全）。

    返回体：success/saved_path/camera_used/annotated... + `_mcp_content`
    （image content block）—— 协议层把 _mcp_content 拆成 image block，
    其余字段进 text block（base64 不进文本，控 token 成本）。
    """
    source = str(args.get("source") or "editor_viewport").strip().lower()
    if source not in ("editor_viewport", "agent_camera", "pie"):
        raise BridgeError(
            'source 非法: %r（可选 "editor_viewport" | "agent_camera" | "pie"）' % source,
            category="param_type")
    try:
        max_dim = int(args.get("max_dimension") or 768)
    except (TypeError, ValueError):
        raise BridgeError("max_dimension 必须为整数", category="param_type")
    max_dim = max(64, min(2048, max_dim))  # 64 下限防 1px 无意义图；2048 上限防上下文爆炸
    annotate = bool(args.get("annotate", True))
    out_name = args.get("out_name") or ""

    camera = args.get("camera") or {}
    if not isinstance(camera, dict):
        raise BridgeError("camera 必须为对象 {pitch, yaw, roll}", category="param_type")
    if source == "agent_camera" and not camera:
        raise BridgeError(
            "source=agent_camera 时 camera {pitch, yaw, roll} 必填（确定性机位）",
            category="param_type",
            suggestion='示例: {"camera": {"pitch": -30, "yaw": 45, "roll": 0}}')

    def _cam3(key, default=0.0):
        try:
            return float(camera.get(key, default))
        except (TypeError, ValueError):
            raise BridgeError("camera.%s 必须为数字" % key, category="param_type")

    # ── ① 发起截图（白名单命令承载；_capture_cmd_dispatch 保证线程规则）──
    if source == "pie":
        launched = _capture_cmd_dispatch("capture_pie", out_path=out_name)
    elif source == "agent_camera":
        launched = _capture_cmd_dispatch(
            "capture_viewport", out_path=out_name, camera_mode="agent_camera",
            pitch=_cam3("pitch"), yaw=_cam3("yaw"), roll=_cam3("roll"))
    else:
        launched = _capture_cmd_dispatch(
            "capture_viewport", out_path=out_name, camera_mode="active_viewport")
    data = launched.get("data") if isinstance(launched.get("data"), dict) else {}
    saved_path = str(data.get("saved_path") or "")
    if not saved_path:
        raise BridgeError("截图命令未回传 saved_path（C++ 侧契约破坏）: %r" % (data,),
                          category="screenshot")

    warnings = list(launched.get("warnings") or [])

    # ── ② 读回验证（存在 + 非空 + 可解码 + 尺寸）—— fail-loud ──
    pil = _load_pillow()
    img, width, height = _pil_load_and_verify(saved_path, pil)
    if not data.get("bytes"):
        # 白名单命令在 pending 轮询后已回填 bytes；直呼路径（非 pending）可能没有 —— 补上
        try:
            data["bytes"] = os.path.getsize(saved_path)
        except OSError:
            pass

    # ── ③ 标注（Pillow 可选：缺失 → 跳过 + 告警，绝不失败）──
    annotated = False
    annotated_actors = []
    if annotate:
        if pil is None:
            warnings.append("Pillow 未安装 —— 跳过 actor 标注（pip install pillow 后可用；"
                            "截图本身不受影响）")
        else:
            proj_names = _screenshot_projection_names(data, source)
            if not proj_names:
                # 名单拿不到（引擎拒绝/世界枚举失败）→ 如实跳过标注，不让标注拖垮截图
                warnings.append("未能枚举场景 actor 名单 —— 跳过标注（可先 find_actors "
                                "再带名单重拍）")
            else:
                proj = _capture_cmd_dispatch(
                    "project_actors_to_screen",
                    actor_names=proj_names,
                    camera_mode="agent_camera" if source == "agent_camera" else "active_viewport",
                    pitch=_cam3("pitch"), yaw=_cam3("yaw"), roll=_cam3("roll"))
                pdata = proj.get("data") if isinstance(proj.get("data"), dict) else {}
                projections = pdata.get("projections") or []
                if isinstance(projections, list) and projections:
                    annotated_actors, notes = _pil_annotate(img, projections, pil)
                    warnings.extend(notes)
                    annotated = bool(annotated_actors)
                    if not annotated:
                        warnings.append("没有 visible=true 的投影（actor 在视锥外或没找到）—— 未画标注")

    # ── ④ 压缩 + base64（长边 max_dimension + JPEG q80；无 Pillow 降级 PNG 直通）──
    mcp_content = []
    returned_mime = ""
    returned_size = [width, height]
    if pil is not None and img is not None:
        jpeg, rw, rh = _pil_compress_jpeg(img, max_dim, pil)
        mcp_content.append({"type": "image", "data": base64.b64encode(jpeg).decode("ascii"),
                            "mimeType": "image/jpeg"})
        returned_mime = "image/jpeg"
        returned_size = [rw, rh]
        if max(width, height) > max_dim:
            warnings.append("返回图已从 %dx%d 压缩到 %dx%d（长边 %d，JPEG q80）；"
                            "全尺寸原图只落盘不进上下文" % (width, height, rw, rh, max_dim))
    else:
        # 无 Pillow：能解码出尺寸且 ≤ max_dimension → 原始 PNG 直出（不压缩不失败）；
        # 超限 → 图不进上下文（base64 大图灌上下文不可接受），只回路径 + 明确告警
        if max(width, height) <= max_dim:
            try:
                with open(saved_path, "rb") as f:
                    raw = f.read()
                mcp_content.append({"type": "image", "data": base64.b64encode(raw).decode("ascii"),
                                    "mimeType": "image/png"})
                returned_mime = "image/png"
                warnings.append("Pillow 未安装 —— 未压缩 PNG 直出（建议 pip install pillow "
                                "以启用压缩与标注）")
            except OSError as e:
                warnings.append("读取 PNG 原图失败，图未进上下文: %s" % e)
        else:
            warnings.append(
                "图像 %dx%d 超过 max_dimension=%d 且 Pillow 未安装（无法压缩）—— "
                "本次不返回图像内容，仅返回落盘路径；安装 Pillow 后重试可获得进上下文的图"
                % (width, height, max_dim))

    # ── ⑤ 组装返回体（camera_used 溯源 = UEBox 经验：图的结论取决于从哪儿拍的）──
    camera_used = {
        "source": source,
        "camera_source": data.get("camera_source"),
        "location": data.get("camera_location"),
        "rotation": data.get("camera_rotation"),
        "fov": data.get("fov"),
        "method": data.get("method"),
    }
    out = {
        "success": True,
        "source": source,
        "saved_path": saved_path,
        "bytes_on_disk": data.get("bytes"),
        "width": width, "height": height,
        "camera_used": camera_used,
        "annotated": annotated,
        "annotated_actors": annotated_actors,
        "returned_mime": returned_mime,
        "returned_size": returned_size,
        "warnings": warnings,
        "_mcp_content": mcp_content,
    }
    return out


def _screenshot_projection_names(data: dict, source: str) -> List[str]:
    """标注投影的 actor 名候选（首版：空表 → C++ 端没有"全场景标注"入口）。

    设计取舍：ue5_screenshot 的 annotate 需要 actor 名单 —— 但模型往往还没查过
    场景。规格书标注流程 = ProjectActorsToScreen(已有名单)。这里用 PIE/编辑器
    世界最近的 find_actors 拿一份小名单（≤12 个）兜底，让"随手一拍"也有标注可看；
    拿不到（引擎拒绝/超时）就如实跳过标注 —— 绝不让标注拖垮截图。
    """
    del source  # world 选择由 find_actors 的 world 参数决定，见下
    try:
        r = _gt_run_handler(_cmd_find_actors, "", "", "pie", 12) \
            if _cmd_pie_state_probe() else _gt_run_handler(_cmd_find_actors, "", "", "editor", 12)
        actors = (r.get("data") or {}).get("actors") or []
        return [str(a.get("name")) for a in actors if isinstance(a, dict) and a.get("name")][:12]
    except Exception:
        return []


def _cmd_pie_state_probe() -> bool:
    """轻量 PIE 探测（标注选世界用；失败一律按编辑器世界 —— 不抛）。"""
    try:
        r = _gt_run_handler(_cmd_pie_state)
        return bool((r.get("data") or {}).get("is_playing"))
    except Exception:
        return False


def _receipt_begin_mcp(name: str, args: dict) -> dict:
    """MCP tools/call 的回执/锁入口（映射工具名到命令与参数）。"""
    args = args if isinstance(args, dict) else {}
    if name == "ue5_execute_command":
        return _receipt_begin(str(args.get("command") or ""),
                              args.get("params") or {})
    mapping = {
        "ue5_connect_pins": ("connect_pins", {"bp_path": args.get("blueprint_path", "")}),
        "ue5_disconnect_pin": ("disconnect_pins", {"asset_path": args.get("blueprint_path", "")}),
        "ue5_delete_node": ("delete_node", {"asset_path": args.get("blueprint_path", "")}),
        "ue5_apply_blueprint_graph": ("apply_graph", {"bp_path": args.get("bp_path", "")}),
        "ue5_delete_asset": ("delete_asset", {"asset_path": args.get("asset_path", "")}),
        "ue5_save_asset": ("save_asset", {"asset_path": args.get("asset_path", "")}),
    }
    if name == "ue5_build_blueprint":
        spec = args.get("spec") or {}
        return _receipt_begin("build_blueprint", {
            "pkg": spec.get("package_path") or spec.get("pkg") or "/Game/Blueprints/",
            "bp_name": spec.get("blueprint_name") or spec.get("name") or ""})
    pair = mapping.get(name)
    if not pair:
        return {}
    return _receipt_begin(pair[0], pair[1])


#: MCP 工具 → 事务命令别名：直连写路径（不走白名单）也各留一个可回滚步。
#  未列出的工具不包事务（只读 / 资产创建 / 保存 / 编译 / PIE 生命周期）。
_MCP_TX_ALIAS = {
    "ue5_connect_pins": "connect_pins",
    "ue5_disconnect_pin": "disconnect_pins",
    "ue5_delete_node": "delete_node",
    "ue5_set_pie_transform": "set_actor_transform",
}


def _shape_fields(value, fields):
    """递归裁剪读结果里的节点/连线字段（fields 为空 → 原样返回）。

    v6.0：上下文治理的"响应塑形"——只对带 `graphs[].nodes/connections` 的
    读结果生效；顶层元数据（world/asset/errors）保留，避免把结构裁没。
    """
    if not fields:
        return value
    keep = {str(f) for f in fields if str(f or "").strip()}

    def _filter_dict(d):
        return {k: v for k, v in d.items() if k in keep}

    def _walk(obj):
        if isinstance(obj, list):
            return [_walk(x) for x in obj]
        if not isinstance(obj, dict):
            return obj
        out = dict(obj)
        graphs = out.get("graphs")
        if isinstance(graphs, list):
            shaped = []
            for g in graphs:
                if not isinstance(g, dict):
                    shaped.append(g)
                    continue
                ng = dict(g)
                if isinstance(ng.get("nodes"), list):
                    ng["nodes"] = [_filter_dict(n) if isinstance(n, dict) else n
                                   for n in ng["nodes"]]
                if isinstance(ng.get("connections"), list):
                    ng["connections"] = [_filter_dict(c) if isinstance(c, dict) else c
                                         for c in ng["connections"]]
                shaped.append(ng)
            out["graphs"] = shaped
        # 任意层级的嵌套都要进（read_blueprint 的图在 data.l2.graphs）
        for k, v in list(out.items()):
            if k == "graphs":
                continue
            if isinstance(v, (dict, list)):
                out[k] = _walk(v)
        return out

    return _walk(value)


def _dt_require_table(asset_path: str):
    """加载并确认是 DataTable（fail-loud，别把普通资产当表）。

    ⚠️ 版本差异：5.8 把 CSV 接口挂在 DataTable 对象上；5.1 只在
    `DataTableFunctionLibrary` 暴露（对象无 export/fill 方法）。
    """
    _pywrap_require_str("asset_path", asset_path)
    pkg = _safe_game_pkg(asset_path)
    if not pkg:
        raise BridgeError("asset_path 非法（必须是规范 /Game/ 路径）: %r"
                          % asset_path, category="param_type")
    obj = unreal.EditorAssetLibrary.load_asset(pkg)
    if obj is None:
        raise BridgeError("资产不存在或加载失败: %s" % asset_path,
                          category="datatable")
    lib = getattr(unreal, "DataTableFunctionLibrary", None)
    is_dt = False
    try:
        is_dt = isinstance(obj, unreal.DataTable)
    except Exception:
        is_dt = False
    if not is_dt:
        try:
            is_dt = str(obj.get_class().get_name()) == "DataTable"
        except Exception:
            is_dt = False
    if not is_dt:
        raise BridgeError("%s 不是 DataTable" % asset_path, category="datatable")
    if not ((hasattr(obj, "fill_from_csv_string") or lib is not None)
            and (hasattr(obj, "get_row_names") or lib is not None)):
        raise BridgeError("%s 是 DataTable，但当前引擎未暴露行/CSV 接口" % asset_path,
                          category="datatable")
    return obj


def _dt_row_names(dt) -> list:
    names = None
    try:
        fn = getattr(dt, "get_row_names", None)
        if callable(fn):
            names = fn()
    except Exception:
        names = None
    if names is None:
        lib = getattr(unreal, "DataTableFunctionLibrary", None)
        fn = getattr(lib, "get_data_table_row_names", None) if lib else None
        if callable(fn):
            names = fn(dt)
    return [str(n) for n in (names or [])]


def _dt_export_csv(dt):
    fn = getattr(dt, "export_to_csv_string", None)
    if callable(fn):
        try:
            return str(fn() or "")
        except Exception:
            return None
    return None


def _dt_fill_csv(dt, csv: str) -> bool:
    fn = getattr(dt, "fill_from_csv_string", None)
    if callable(fn):
        return bool(fn(csv))
    lib = getattr(unreal, "DataTableFunctionLibrary", None)
    fn = getattr(lib, "fill_data_table_from_csv_string", None) if lib else None
    if callable(fn):
        return bool(fn(dt, csv))
    raise BridgeError("当前引擎未暴露 CSV 写入接口", category="datatable")


def _cmd_datatable_read(asset_path: str, limit: int = 50,
                        include_csv: bool = True) -> dict:
    """读 DataTable：行名 + 结构体 + CSV（可直接交给 datatable_write_csv 改）。"""
    try:
        limit = max(1, min(int(limit), 5000))
    except (TypeError, ValueError):
        limit = 50

    def _do():
        dt = _dt_require_table(asset_path)
        names = _dt_row_names(dt)
        csv_text = _dt_export_csv(dt)
        struct_name = ""
        try:
            st = dt.get_row_struct()
            struct_name = str(st.get_path_name()) if st else ""
        except Exception:
            pass
        out = {"asset": asset_path, "row_count": len(names),
               "row_names": names[:limit], "truncated": len(names) > limit,
               "row_struct": struct_name,
               "csv_supported": csv_text is not None,
               "csv_chars": len(csv_text or "")}
        if include_csv and csv_text is not None:
            # 上限 200k 字符：大表只给行名，避免把上下文灌爆
            out["csv"] = csv_text[:200000]
            out["csv_truncated"] = len(csv_text) > 200000
        elif include_csv:
            out["warnings"] = ["当前引擎未暴露 CSV 导出（5.1）；只返回行名"]
        return out

    data = _gt_run_handler(_do, timeout=60.0)
    return {"success": True, "ok": True, "data": data}


def _cmd_datatable_write_csv(asset_path: str, csv: str,
                             confirm: bool = False) -> dict:
    """用 CSV 覆盖 DataTable 行（需 confirm=true）；写后回读行名作为证据。"""
    if not confirm:
        raise BridgeError("datatable_write_csv 会覆盖表内容，需 confirm=true",
                          category="destructive_guard")
    if not isinstance(csv, str) or not csv.strip():
        raise BridgeError("csv 不能为空（至少表头一行）", category="param_type")
    if len(csv) > 5 * 1024 * 1024:
        raise BridgeError("csv 超过 5MB 上限（请拆分表或改用文件导入）",
                          category="param_type")

    def _do():
        dt = _dt_require_table(asset_path)
        before = len(_dt_row_names(dt))
        ok = _dt_fill_csv(dt, csv)
        after_names = _dt_row_names(dt)
        out = {"asset": asset_path, "filled": ok, "rows_before": before,
               "rows_after": len(after_names),
               "row_names": after_names[:50]}
        if not ok:
            out["_fail_loud"] = True
            out["error"] = "fill_from_csv_string 返回 False（CSV 与行结构不匹配？）"
            return out
        saved = bool(unreal.EditorAssetLibrary.save_asset(
            str(asset_path).split(".")[0]))
        out["saved"] = saved
        if not saved:
            out["_fail_loud"] = True
            out["error"] = "行已写入内存但保存失败（重启会丢失）"
        return out

    data = _gt_run_handler(_do, timeout=120.0)
    ok = bool(data.get("filled")) and bool(data.get("saved"))
    return {"success": ok, "ok": ok, "data": data}


def _cmd_datatable_create(asset_path: str, row_struct: str,
                          package_path: str = "") -> dict:
    """新建 DataTable（指定行结构）；用于测试/脚手架（真实项目多用导入）。"""
    _pywrap_require_str("asset_path", asset_path)
    _pywrap_require_str("row_struct", row_struct)
    full = str(asset_path).strip().split(".")[0]
    if not _safe_game_pkg(full):
        raise BridgeError("asset_path 需为规范 /Game/... 包路径（禁止 ..）",
                          category="param_type")
    pkg = str(package_path or "").strip() or full.rsplit("/", 1)[0]
    name = full.rsplit("/", 1)[-1]

    def _do():
        if unreal.EditorAssetLibrary.does_asset_exist(full):
            raise BridgeError("资产已存在: %s" % full, category="datatable")
        st = None
        try:
            st = unreal.load_object(None, row_struct)
        except Exception:
            st = None
        if st is None:
            st = unreal.load_class(None, row_struct)
        if st is None:
            raise BridgeError("行结构不可解析: %s（示例 /Script/GameplayTags."
                              "GameplayTagTableRow）" % row_struct,
                              category="datatable")
        factory = unreal.DataTableFactory()
        for prop in ("struct", "struct_type", "row_struct"):
            try:
                factory.set_editor_property(prop, st)
                break
            except Exception:
                continue
        dt = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            name, pkg, unreal.DataTable, factory)
        if dt is None:
            raise BridgeError("create_asset 返回 None（DataTable 未创建）",
                              category="datatable")
        unreal.EditorAssetLibrary.save_asset(full)
        return {"asset": full, "row_struct": str(st.get_path_name())}

    data = _gt_run_handler(_do, timeout=60.0)
    if _TASK_JOURNAL_AVAILABLE and _task_journal is not None:
        try:
            _task_journal.note_created_asset(full)
        except Exception:
            pass
    return {"success": True, "ok": True, "data": data}


# ══════════════════════════════════════════════════════════════════
# v6.5 · AnimBP 只读通道（probe / read）
# ══════════════════════════════════════════════════════════════════
# 只读是明确边界：不同的 UE 版本暴露的 AnimGraph 属性不同；读不到必须
# fail-loud，绝不宣称可写。写入通道需真实 AnimBP 资产做真机验证后再开。
_ANIMBP_PROBE_CLASSES = (
    "AnimBlueprint", "AnimBlueprintFactory", "AnimGraphNode_StateMachine",
    "AnimGraphNode_StateMachineBase", "AnimStateNode", "AnimStateTransitionNode",
    "AnimStateAliasNode", "AnimGraphNode_Root", "AnimGraphNode_LinkedAnimLayer",
)
_ANIMBP_GRAPH_PROPS = ("FunctionGraphs", "UbergraphPages", "AnimGraphs", "AnimGraph",
                       "function_graphs", "ubergraph_pages", "anim_graphs",
                       "anim_graph")
_ANIMBP_NODE_PROPS = ("Nodes", "nodes")


def _animbp_load(asset_path: str):
    u = globals().get("unreal")
    if u is None:
        raise BridgeError("unreal 模块不可用", category="bridge_unavailable")
    path = str(asset_path or "").strip()
    if not path:
        raise BridgeError("asset_path 不能为空", category="param_type")
    if len(path) > 512:
        raise BridgeError("asset_path 过长（上限 512 字符）", category="param_type")
    pkg = _safe_game_pkg(path)
    if not pkg:
        raise BridgeError("asset_path 非法（只允许 /Game/ 下路径）: %r" % (path,),
                          category="param_type")
    eal = getattr(u, "EditorAssetLibrary", None)
    if eal is None or not hasattr(eal, "load_asset"):
        raise BridgeError("EditorAssetLibrary.load_asset 不可用", category="animbp")
    asset = eal.load_asset(pkg)
    if not asset:
        raise BridgeError("资产不存在或无法加载: %s" % pkg, category="asset_load")
    return asset, pkg


def _animbp_class_name(asset) -> str:
    try:
        cls = asset.get_class()
        if cls is not None:
            return str(cls.get_name())
    except Exception:
        pass
    return type(asset).__name__


def _animbp_read_prop(obj, names):
    for name in names:
        try:
            return name, obj.get_editor_property(name)
        except Exception:
            pass
        try:
            return name, getattr(obj, name)
        except Exception:
            pass
    return None, None


def _animbp_ref(value) -> str:
    """对象引用 → 短路径（避免把 <Object ... 0x...> 原样吐给 agent）。"""
    try:
        p = value.get_path_name()
        if p:
            return str(p)[:200]
    except Exception:
        pass
    return str(value)[:200]


def _cmd_animbp_probe(asset_path: str) -> dict:
    """只读：资产类型 / 父类 / 骨骼 / 动画类能力（不修改任何东西）。"""
    def _do():
        asset, path = _animbp_load(asset_path)
        cls = _animbp_class_name(asset)
        info = {"asset_path": path, "class": cls,
                "is_animbp": "AnimBlueprint" in cls}
        k, v = _animbp_read_prop(asset, ("ParentClass", "parent_class"))
        if not k:
            fn = getattr(asset, "get_parent_class", None)
            if callable(fn):
                try:
                    v, k = fn(), "get_parent_class"
                except Exception:
                    pass
        if k:
            info["parent_class"] = _animbp_ref(v)
        k, v = _animbp_read_prop(asset, ("TargetSkeleton", "target_skeleton",
                                         "Skeleton", "skeleton"))
        if k:
            info["target_skeleton"] = _animbp_ref(v)
        u = globals().get("unreal")
        info["capabilities"] = {
            name: bool(u is not None and hasattr(u, name))
            for name in _ANIMBP_PROBE_CLASSES
        }
        info["apis"] = {
            "get_animation_graphs": callable(
                getattr(asset, "get_animation_graphs", None)),
        }
        info["reads"] = {
            "class": True,
            "parent_class": "parent_class" in info,
            "target_skeleton": "target_skeleton" in info,
            "graphs": info["apis"]["get_animation_graphs"],
        }
        info["writes_supported"] = False
        if not info["is_animbp"]:
            info["warning"] = "资产不是 AnimBlueprint（class=%s）" % cls
        return info

    return {"success": True, "ok": True,
            "data": _gt_run_handler(_do, timeout=30.0)}


def _cmd_animbp_read(asset_path: str, include_nodes: bool = True,
                     limit: int = 500) -> dict:
    """只读：枚举 AnimBP 的图/节点（能读多少报多少；读不到 fail-loud）。"""
    try:
        limit = max(1, min(int(limit), 2000))
    except (TypeError, ValueError):
        limit = 500

    def _do():
        asset, path = _animbp_load(asset_path)
        cls = _animbp_class_name(asset)
        if "AnimBlueprint" not in cls:
            raise BridgeError("不是 AnimBlueprint: %s（class=%s）" % (path, cls),
                              category="animbp")
        graph_objs = []
        # 5.8 实测：get_animation_graphs() 是公开 API；UPROPERTY 多为 protected
        fn = getattr(asset, "get_animation_graphs", None)
        if callable(fn):
            try:
                val = fn()
                if val is not None:
                    graph_objs = list(val)
            except Exception:
                graph_objs = []
        if not graph_objs:
            for prop in _ANIMBP_GRAPH_PROPS:
                try:
                    val = asset.get_editor_property(prop)
                except Exception:
                    continue
                if isinstance(val, (list, tuple)):
                    graph_objs.extend(list(val))
                    break
        graphs = []
        any_nodes_unavailable = False
        for g in graph_objs:
            if len(graphs) >= 50:
                break
            gname = ""
            try:
                gname = str(g.get_name())
            except Exception:
                pass
            ginfo = {"name": gname, "node_count": 0, "nodes_available": False}
            if include_nodes:
                nodes = None
                for np in _ANIMBP_NODE_PROPS + ("get_nodes",):
                    try:
                        if np == "get_nodes":
                            nodes = g.get_nodes()
                        else:
                            nodes = g.get_editor_property(np)
                        break
                    except Exception:
                        continue
                out_nodes = []
                if isinstance(nodes, (list, tuple)):
                    ginfo["nodes_available"] = True
                    for n in list(nodes)[:limit]:
                        nd = {"class": type(n).__name__}
                        for attr in ("get_node_title", "get_name"):
                            try:
                                t = getattr(n, attr)()
                                if t:
                                    nd["title"] = str(t)
                                    break
                            except Exception:
                                pass
                        try:
                            nd["guid"] = str(n.get_editor_property("NodeGuid"))
                        except Exception:
                            pass
                        out_nodes.append(nd)
                else:
                    any_nodes_unavailable = True
                ginfo["nodes"] = out_nodes
                ginfo["node_count"] = len(out_nodes)
            graphs.append(ginfo)
        data = {"asset_path": path, "class": cls, "graph_count": len(graphs),
                "graphs": graphs, "supported": bool(graphs)}
        if any_nodes_unavailable:
            data["warning"] = ("图已枚举，但节点级属性未暴露（nodes_available=false）—— "
                               "只读降级，未做任何修改")
        if graphs:
            data.update({"success": True, "ok": True})
        else:
            data.update({
                "success": False, "ok": False, "_fail_loud": True,
                "error": ("当前 unreal Python 未暴露 AnimBP 图属性（%s）—— "
                          "只读枚举失败，未做任何修改"
                          % ", ".join(_ANIMBP_GRAPH_PROPS)),
                "note": "先用 animbp_probe 看类能力；写入通道未启用（需真机验证）",
            })
        return data

    return _gt_run_handler(_do, timeout=60.0)


def _cmd_class_probe(names, include_members: bool = False) -> dict:
    """只读能力探针：检查 unreal 下某批类/工厂/方法是否存在（排查用）。

    names: ["AnimBlueprintFactory", "AnimGraphNode_StateMachine", ...]
    返回 {name: {"exists": bool, "kind": "...", "members": [...]?}}。
    """
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list) or not names:
        raise BridgeError("names 必须为非空数组", category="param_type")

    def _do():
        u = globals().get("unreal")
        out = {}
        for raw in names[:50]:
            name = str(raw or "").strip()
            if not name:
                continue
            obj = getattr(u, name, None) if u is not None else None
            info = {"exists": obj is not None,
                    "kind": type(obj).__name__ if obj is not None else None}
            if obj is not None and include_members:
                try:
                    info["members"] = [m for m in dir(obj)
                                       if not m.startswith("_")][:80]
                except Exception:
                    info["members"] = []
            out[name] = info
        return out

    data = _gt_run_handler(_do, timeout=30.0)
    return {"success": True, "ok": True, "data": data}


def _mcp_impl_dispatch(name, args):
    """工具名 → 实现（未列出的名字返回 None）。"""
    if not isinstance(args, dict):
        args = {}
    simple = {
        "ue5_check_health": lambda a: _get_ue_info(),
        "ue5_run_diagnostics": lambda a: bridge_diag(),
        "ue5_list_assets": lambda a: _cmd_list_assets(a.get("path", "/Game/"),
                                                      a.get("recursive", True)),
        "ue5_read_blueprint": lambda a: _shape_fields(read_blueprint(
            ReadRequest(blueprint_path=a["blueprint_path"],
                        level=a.get("level", "L1"),
                        include_cdo=a.get("include_cdo", True),
                        include_connections=a.get("include_connections", True)),
            _TrustedRequest()), a.get("fields")),
        "ue5_read_nodes": lambda a: _shape_fields(
            _cmd_read_nodes(a["bp_path"], a.get("graph_name")), a.get("fields")),
        "ue5_read_connections": lambda a: _shape_fields(
            _cmd_read_connections(a["bp_path"], a.get("graph_name", "EventGraph")),
            a.get("fields")),
        "ue5_build_blueprint": _mcp_impl_build,
        "ue5_build_batch": _mcp_impl_build_batch,
        "ue5_compile_blueprint": lambda a: _cmd_compile_blueprint(a["bp_path"]),
        # T-20261024-UE5BRIDGE-COMPILE-DIAGNOSE：结构化编译诊断（修复循环的反馈侧）
        "ue5_compile_and_diagnose": lambda a: _cmd_compile_and_diagnose(a["bp_path"]),
        # T-20261024-UE5BRIDGE-GRAPH-APPLY：整图声明式写入（写图主路径，
        # search 先拿签名 → apply 原子应用；逐节点工具仅手术式微调）
        "ue5_search_blueprint_nodes": lambda a: _cmd_search_blueprint_nodes(
            a["bp_path"], a.get("query", ""), a.get("limit", 20)),
        "ue5_apply_blueprint_graph": _mcp_impl_apply,
        "ue5_connect_pins": lambda a: _gt_run_handler(
            _cmd_connect_pins, a["blueprint_path"], a.get("connections", [])),
        "ue5_disconnect_pin": lambda a: disconnect_pin(
            DisconnectRequest(blueprint_path=a["blueprint_path"],
                              node_id=a["node_id"], pin_name=a["pin_name"]),
            _TrustedRequest()),
        "ue5_delete_node": lambda a: delete_node(
            DeleteNodeRequest(blueprint_path=a["blueprint_path"],
                              node_id=a["node_id"], dry_run=a.get("dry_run", False)),
            _TrustedRequest()),
        "ue5_save_asset": lambda a: _cmd_save_asset(
            a["asset_path"], a.get("only_if_is_dirty", False)),
        "ue5_delete_asset": lambda a: _cmd_delete_asset(
            a["asset_path"], a.get("max_attempts", 3)),
        # T-20261024-UI-V2：任务上报（仪表盘任务脉络的数据源）
        "ue5_task_begin": lambda a: _cmd_task_begin(a),
        "ue5_task_step": lambda a: _cmd_task_step(a),
        "ue5_task_end": lambda a: _cmd_task_end(a),
        "ue5_task_list": lambda a: _cmd_task_list(),
        "ue5_start_pie": lambda a: _cmd_pie_start(a.get("mode", "simulate")),
        "ue5_stop_pie": lambda a: _cmd_pie_stop(),
        "ue5_read_pie_state": lambda a: _cmd_pie_state(),
        "ue5_list_pie_actors": lambda a: _cmd_find_actors(
            a.get("actor_class", ""), a.get("name_filter", ""), "pie",
            a.get("limit", 100)),
        "ue5_read_pie_property": lambda a: _cmd_get_actor_properties(
            a["actor_name"], a.get("property_name", ""), "pie", 200),
        "ue5_set_pie_transform": lambda a: _cmd_set_actor_transform(
            a["actor_name"], a.get("location"), a.get("rotation"), "pie"),
        "ue5_read_logs": lambda a: _cmd_logs_read(
            a.get("lines", 200), a.get("category", ""), a.get("pattern", ""),
            a.get("cursor", 0)),
        # T-20261024-UE5BRIDGE-SCREENSHOT：截图反馈闭环（视觉自检的"眼睛"）
        # ⚠️ 本工具由 _mcp_handle_one 特判为 HTTP 线程直呼（见该处注释的死锁原理）
        "ue5_screenshot": _mcp_impl_screenshot,
        "ue5_run_batch": lambda a: _cmd_editor_batch(
            a["steps"], a.get("stop_on_error", True)),
        "ue5_execute_command": _mcp_impl_command,
        "ue5_verify_scenario": lambda a: _cmd_verify_scenario(
            a["name"], a["steps"], a.get("start_pie", True),
            a.get("pie_mode", "simulate"), a.get("stop_pie", True),
            a.get("junit_path", ""), a.get("stop_on_failure", True),
            a.get("timeout_s", 300.0), a.get("allow_destructive", False)),
        "ue5_verify_behavior": lambda a: _cmd_verify_behavior(
            a["actor_name"], a["property_name"], a.get("expected", ""),
            a.get("world", "pie"), a.get("match", "exact"),
            a.get("tolerance", 0.0), a.get("capture", True),
            a.get("out_path", "")),
        "ue5_tool_search": lambda a: _cmd_tool_search(
            a.get("query", ""), a.get("limit", 8),
            a.get("include_tools", True)),
    }
    return simple.get(name)


def _mcp_err(_id, code, message):
    return {"jsonrpc": "2.0", "id": _id,
            "error": {"code": code, "message": message}}


# ══════════════════════════════════════════════════════════
#  调用守卫：死循环保护 + 高危操作确认（T-20261027-GUARD）
# ══════════════════════════════════════════════════════════
#  死循环保护（对标 UEBox loopBreaker，规则极简）：
#    · 同一「工具 + 参数」连续失败 2 次即拦截；编译类放宽到 5 次
#      （"改 → 编译 → 再改"是正常修错循环，不能误杀）；
#    · 任何一次成功调用清零全部计数（成功就说明在推进）。
#  高危确认（不破面板只读边界，用参数级二次确认）：
#    · delete_asset / delete_node / unpin_console_refs，以及
#      apply_graph{replace:true}（会清空目标图）→ 必须显式 confirm=true；
#    · 未带 confirm 直接拒绝并说明原因 —— 防 agent 误删/误清空。
_LOOP_STREAK: Dict[str, list] = {}      # key -> [count, last_ts]
_LOOP_STREAK_WINDOW_S = 300.0           # 超过 5 分钟未再失败则重置（防长期累积误拦）
_LOOP_LOCK = threading.Lock()
_LOOP_LIMIT_DEFAULT = 2
_LOOP_LIMIT_VOLATILE = 5
_LOOP_VOLATILE = {"ue5_compile_and_diagnose", "ue5_compile_blueprint",
                  "ue5_verify_scenario"}
_DESTRUCTIVE_TOOLS = {"ue5_delete_asset", "ue5_delete_node"}
_DESTRUCTIVE_CMDS = {"delete_asset", "delete_node", "unpin_console_refs",
                     "batch_asset_ops", "rollback_task_assets",
                     "datatable_write_csv"}


def _arg_fingerprint(args) -> str:
    try:
        raw = json.dumps(args, sort_keys=True, ensure_ascii=False)
    except Exception:
        raw = str(args)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def _guard_call(name: str, args) -> str:
    """调用前守卫 → 返回 loop key（供调用后记结果）。抛 BridgeError 即拦截。"""
    args = args if isinstance(args, dict) else {}
    cmd = str(args.get("command") or "") if name == "ue5_execute_command" else name
    is_dest = name in _DESTRUCTIVE_TOOLS or cmd in _DESTRUCTIVE_CMDS
    if name == "ue5_apply_blueprint_graph" and args.get("replace"):
        is_dest = True
    # batch_asset_ops 的 dry-run 是只读预检（默认），不需要 confirm；真跑才强制
    if cmd == "batch_asset_ops":
        _p = args.get("params") if isinstance(args.get("params"), dict) else {}
        _dry = args.get("dry_run", _p.get("dry_run", True))
        if _dry is not False:
            is_dest = False
    # confirm 允许两种传法：arguments 顶层或嵌套在 params 里（透传壳的自然写法）
    _confirm = bool(args.get("confirm"))
    if not _confirm and isinstance(args.get("params"), dict):
        _confirm = bool(args["params"].get("confirm"))
    if is_dest and not _confirm:
        raise BridgeError(
            "%s 是破坏性操作（会删除/清空已有内容），需显式 confirm=true 重试"
            % (cmd or name),
            category="destructive_guard",
            suggestion="确认目标无误后，同一调用加 confirm=true 再执行")
    key = name + "|" + _arg_fingerprint(args)
    with _LOOP_LOCK:
        entry = _LOOP_STREAK.get(key) or [0, 0.0]
        if time.time() - float(entry[1] or 0) > _LOOP_STREAK_WINDOW_S:
            entry = [0, 0.0]
        streak = int(entry[0])
    limit = _LOOP_LIMIT_VOLATILE if name in _LOOP_VOLATILE else _LOOP_LIMIT_DEFAULT
    if streak >= limit:
        raise BridgeError(
            "同一调用已连续失败 %d 次（%s），已拦截以免空转" % (streak, name),
            category="loop_guard",
            suggestion="换方法：先读失败原因/改参数/先 search 拿准确签名/"
                       "或把情况告诉用户重定方向")
    return key


def _note_call_result(key: str, ok: bool) -> None:
    """调用后记结果：成功清零全部计数；失败累加当前 key。"""
    if not key:
        return
    try:
        with _LOOP_LOCK:
            if ok:
                _LOOP_STREAK.clear()      # 任何成功都清零（在推进）
            else:
                cur = _LOOP_STREAK.get(key) or [0, 0.0]
                _LOOP_STREAK[key] = [int(cur[0]) + 1, time.time()]
    except Exception:
        pass


def _mcp_handle_one(payload: Dict):
    """处理单条 JSON-RPC 消息 → (response_dict|None, http_status)。"""
    if not isinstance(payload, dict):
        return _mcp_err(None, -32700, "Parse error: payload is not an object"), 200
    _id = payload.get("id", None)
    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    if method.startswith("notifications/"):
        return None, 202
    if method == "initialize":
        client_v = str(params.get("protocolVersion") or "")
        v = client_v if client_v in _MCP_PROTOCOLS else _MCP_PROTOCOLS[1]
        return {"jsonrpc": "2.0", "id": _id,
                "result": {"protocolVersion": v,
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": _MCP_SERVER_INFO}}, 200
    if method == "ping":
        return {"jsonrpc": "2.0", "id": _id, "result": {}}, 200
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": _id,
                "result": {"tools": _mcp_tool_defs_core()}}, 200
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": _id, "result": {"resources": []}}, 200
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": _id, "result": {"prompts": []}}, 200
    if method == "tools/call":
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        impl = _mcp_impl_dispatch(name, args)
        if impl is None:
            return _mcp_err(_id, -32602, "Unknown tool: %s" % name), 200
        try:
            _guard_key = _guard_call(name, args)
            _rctx = _receipt_begin_mcp(name, args)
            # ue5_screenshot 特判（T-20261024-UE5BRIDGE-SCREENSHOT）：【HTTP 线程直呼】，
            # 绝不整体包进 _gt_run_handler —— pending 截图（FScreenshotRequest 延迟一帧）
            # 的文件由帧推进产生，GT worker 上轮询 = 阻塞帧推进 = 永远等不到（自死锁）。
            # 它的 GT 触达点（白名单命令 → bridge.capture_*）内部经 _gt_run_handler
            # 自行分流；C++ 侧自带 GameThread guard。其余工具照旧统一分流。
            if name in ("ue5_screenshot", "ue5_verify_scenario"):
                result = impl(args)
            elif (name == "ue5_execute_command"
                  and str(args.get("command") or "") == "verify_scenario"):
                # 透传壳里的场景也必须走 HTTP 线程（内部等待/轮询不能占 GT）
                result = impl(args)
            else:
                # MCP 工具直连写路径（不走白名单）也要包事务：
                # 手术式连线/断线/删节点/改 Actor 各留一个可回滚步。
                _tx_cmd = _MCP_TX_ALIAS.get(name)
                if _tx_cmd:
                    result = _gt_run_handler(_dispatch_args_with_tx,
                                            _tx_cmd, impl, args)
                else:
                    result = _gt_run_handler(impl, args)
            # 截图闭环：GT 上下文里发起的 pending 截图在【HTTP 线程】收尾轮询
            result = _capture_finish_pending(result)
            result = _receipt_finish(_rctx, result)
            # ── T-20261024-UE5BRIDGE-DASHBOARD：图纸快照/验证卡片挂钩 ──
            # read 族 → 图纸快照缓存；apply → GraphSpec 快照 + node_guids；
            # compile 族 → 编译错误钉；写族 → 验证卡片。fail-open（观测面
            # 例外声明见 add_trace_id 内注释），绝不影响工具返回体。
            try:
                _ui_dash.observe_tool_result(name, args, result)
            except Exception:
                pass
            # 同时认 `success` 与 `ok` 两种成功键：蓝图节点/变量/组件族（36 条写
            #   命令）的返回体只有 `ok`（`_pywrap_ctor_result` / `_pywrap_node_result`），
            #   原先只判 `success` → 这些命令一旦改成"返回 ok:False 而非 raise"，
            #   就会以 `isError:false` 静默假成功。当前它们靠 raise 兜住，此处把
            #   隐式契约显式化。
            # ue5_screenshot：_mcp_content（image block）拆出，base64 不进文本块
            #   （控 token 成本）；其余字段进 text block。
            is_err = (isinstance(result, dict)
                      and (result.get("success") is False
                           or result.get("ok") is False))
            _note_call_result(_guard_key, not is_err)
            content = []
            if isinstance(result, dict) and result.get("_mcp_content"):
                content.extend(result["_mcp_content"])
                json_view = {k: v for k, v in result.items() if k != "_mcp_content"}
            else:
                json_view = result
            content.append({"type": "text", "text": _safe_json(json_view)})
            return {"jsonrpc": "2.0", "id": _id,
                    "result": {"content": content,
                               "isError": bool(is_err)}}, 200
        except Exception as e:
            _note_call_result(locals().get("_guard_key", ""), False)
            # ── T-20261024-UE5BRIDGE-DASHBOARD：失败也进验证墙 ──
            # 实现抛异常（409 守卫 / 蓝图不存在 / 引擎拒绝…）→ 合成失败返回体
            # 喂给观测面。"失败置顶红色"的前提是失败可见 —— raise 路径若漏记，
            # 验证墙只会报喜（这正是 UEBox 纪律要防的假成功，观测面同罪）。
            try:
                _ui_dash.observe_tool_result(name, args, {
                    "success": False, "ok": False, "verified": False,
                    "errors": ["%s: %s" % (type(e).__name__, e)]})
            except Exception:
                pass
            return {"jsonrpc": "2.0", "id": _id,
                    "result": {"content": [{"type": "text",
                                            "text": "%s: %s" % (type(e).__name__, e)}],
                               "isError": True}}, 200
    return _mcp_err(_id, -32601, "Method not found: %s" % method), 200


def _trace_command(payload, resp, status, elapsed_ms: float = 0.0) -> None:
    """把「下发给工具的命令」记进任务流水（fail-open，T-20261025-UX）。

    在 MCP 端点层挂钩（此处 body 已解析、能拿到 name/arguments/结果）——
    面板因此能显示：下发了什么命令、参数摘要、UE 返回什么、UE 内实际动作。
    """
    try:
        if not _TASK_JOURNAL_AVAILABLE or not isinstance(payload, dict):
            return
        if payload.get("method") != "tools/call":
            return
        params = payload.get("params") or {}
        name = str(params.get("name") or "")
        if not name:
            return
        args_in = params.get("arguments") or {}
        explain_key = name
        # 透传壳（ue5_execute_command）：流水里显示**真实子命令**（add_variable 等），
        # 这样面板能按计划步骤的 hint 归位，也不会让用户看到一堆 execute_command
        if name == "ue5_execute_command":
            real = str(args_in.get("command") or "").strip()
            if real:
                name = real
                explain_key = "ue5_" + real
        result = None
        try:
            for block in (resp or {}).get("result", {}).get("content", []):
                if block.get("type") == "text":
                    result = json.loads(block.get("text"))
                    break
        except Exception:
            result = None
        _task_journal.note_command(
            name, args_in, result,
            ok=(int(status or 0) < 400), latency_ms=float(elapsed_ms or 0.0),
            explain_key=explain_key)
    except Exception:
        pass  # fail-open


@app.post("/mcp")
def mcp_endpoint(payload: Dict = Body(default=None), request: Request = None):
    """MCP 协议端点（Streamable HTTP · JSON-RPC 2.0）。

    支持单条与批量消息（数组）；通知类返回 202 空体。
    """
    if not _verify_token(request):
        _OP_LOG.warning("❌ /mcp: 未授权访问 (token 不匹配)")
        raise HTTPException(status_code=403,
                            detail="Unauthorized: invalid or missing token")
    # ── T-20261024-UE5BRIDGE-DASHBOARD：tool_name 挂钩 ──
    # 在 MCP 处理器内（body 已被 FastAPI 解析）提取 tool_name 写到 request.state，
    # 中间件收尾时读取 —— 刻意不在中间件读 body（流式 body 只能读一次）。
    if _DASHBOARD_AVAILABLE:  # 观测面降级时跳过（无仪表盘必须照常无头运行）
        _ui_dash.note_tool_name(request, _ui_dash.tool_names_from_mcp_payload(payload))
    _sid = ""
    try:
        _sid = str(request.headers.get("mcp-session-id", "") or "")
    except Exception:
        pass
    if not _sid:
        _sid = uuid.uuid4().hex
    headers = {"Mcp-Session-Id": _sid}
    if isinstance(payload, list):
        responses = []
        status = 200
        for item in payload:
            _t0b = time.perf_counter()
            resp, st = _mcp_handle_one(item)
            _trace_command(item, resp, st, (time.perf_counter() - _t0b) * 1000.0)
            if resp is not None:
                responses.append(resp)
            status = max(status, st) if st != 202 else status
        if not responses:
            return JSONResponse(status_code=202, content={}, headers=headers)
        return JSONResponse(status_code=200, content=responses, headers=headers)
    _t0 = time.perf_counter()
    resp, status = _mcp_handle_one(payload or {})
    _trace_command(payload or {}, resp, status,
                   (time.perf_counter() - _t0) * 1000.0)
    if resp is None:
        return JSONResponse(status_code=202, content={}, headers=headers)
    return JSONResponse(status_code=status, content=resp, headers=headers)


# ══════════════════════════════════════════════════════════
#  /ui 只读观测面（T-20261024-UE5BRIDGE-DASHBOARD · 管理端仪表盘 v1）
# ══════════════════════════════════════════════════════════
#  定位（规格书《管理端仪表盘设计》）：给"可验证自动化"装上可见的仪表盘。
#  · **永久只读**：本节全部 GET，无任何写路由（tests/test_dashboard.py 遍历
#    app.routes 锁定该契约）；不做聊天/编辑器控制/登录/多用户。
#  · 鉴权复用 _verify_token（X-Skill-Token / Authorization: Bearer / ?token=）。
#    ★ /ui* 专用放宽：token 允许走查询参数 —— 页面是浏览器直接打开的静态单页，
#      无法配自定义 header；本服务只绑 127.0.0.1（start() 强制 localhost），
#      面板全部只读 GET，token 泄露面 = 本机浏览器历史，风险可控
#      （与 _verify_token 内 ?token= 兜底同一条安全注释口径）。
#  · 鉴权失败返回 401（区别于业务端点的 403：/ui 是浏览器人面，401 + 提示
#    "缺 token" 对人类更友好；语义上"未认证"也更准确）。
#  · 观测面整组降级：ui_dashboard 导入失败时本节端点返回 503（观测面不可用
#    不许影响命令面 —— 同 fail-open 纪律的端点级表达）。
# ══════════════════════════════════════════════════════════

def _ui_auth(request: Request) -> None:
    """/ui* 统一鉴权：失败抛 401（浏览器人面口径，理由见上节注释）。"""
    if not _DASHBOARD_AVAILABLE:
        raise HTTPException(status_code=503,
                            detail="仪表盘数据管道不可用（ui_dashboard 导入失败），命令面不受影响")
    if not _verify_token(request):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: 仪表盘只读面需要 token —— "
                   "URL 加 ?token=<UE5_BRIDGE_TOKEN>，或请求头 X-Skill-Token")


@app.get("/ui")
def ui_page(request: Request = None):
    """仪表盘静态页（单文件 HTML+原生 JS/CSS，无构建链 —— 规格书边界）。

    FileResponse 直发单文件即可：只有一个页面，不值得上 StaticFiles 挂载
    （挂载会引入 Mount 路由类型，徒增只读性审计成本）。

    v6.0.1：**静态壳不鉴权**（HTML 不含数据）；所有 /ui/api/* 仍强制 token。
    否则无 token 打开会得到 401 JSON —— 用户连"粘贴 token"的入口都看不到。
    """
    if not _UI_INDEX or not os.path.isfile(_UI_INDEX):
        raise HTTPException(status_code=404,
                            detail=f"ui/index.html 缺失: {_UI_INDEX}")
    return FileResponse(_UI_INDEX, media_type="text/html; charset=utf-8")


@app.get("/ui/api/events")
def ui_api_events(request: Request = None, since: int = 0, limit: int = 500,
                  verdicts_since: int = 0):
    """增量事件轮询（页面 500ms 一次；v1 不上 SSE —— 规格书边界）。

    · since=<事件 id 游标>：只回 id > since 的事件（旧→新）；
    · verdicts_since=<验证卡片游标>：写操作 verified/warnings 卡片（验证墙）；
    · 返回 last_id / last_verdict_id 作为下一轮游标（无新事件也回，防缓冲
      淘汰导致游标回退）。
    """
    _ui_auth(request)
    events, last_id = _ui_dash.events_since(since, limit)
    verdicts, last_vid = _ui_dash.verdicts_since(verdicts_since)
    return {
        "events": events,
        "last_id": last_id,
        "verdicts": verdicts,
        "last_verdict_id": last_vid,
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/ui/api/graph")
def ui_api_graph(request: Request = None, asset: str = ""):
    """最近图纸快照（reader L2 / GraphSpec 归一形态；缓存最近 8 个资产）。

    · asset 缺省 → 最近更新的一个快照；
    · 未命中 → 404 + 当前缓存资产清单（页面据此提示"先 read 一次"）。
    快照来源：/read 端点与 /mcp 的 read/apply 工具（observe_* 挂钩），
    页面渲染坐标优先 GraphSpec/reader 既有 pos，没有则前端简单分层。
    """
    _ui_auth(request)
    snap = _ui_dash.get_graph_snapshot(asset)
    if snap is None:
        return JSONResponse(status_code=404, content={
            "found": False,
            "detail": "无图纸快照：先对目标蓝图执行 read_nodes/read_blueprint"
                      "（L2）或 apply_blueprint_graph",
            "assets": _ui_dash.snapshot_assets(),
        })
    return {"found": True, "snapshot": snap, "assets": _ui_dash.snapshot_assets()}


@app.get("/ui/api/reports")
def ui_api_reports(request: Request = None):
    """报告列表（skill output/）+ JSONL 会话清单（审计导出的内容预览）。"""
    _ui_auth(request)
    return {"reports": _ui_dash.list_reports(), "sessions": _ui_dash.list_sessions()}


@app.get("/ui/api/reports/export")
def ui_api_reports_export(request: Request = None):
    """审计导出：JSONL 会话 + 报告文件打 zip（页面 window.open 触发下载）。

    定位兑现（规格书）：审计导出件 = 工作室技术评审的实物凭证 ——
    "可验证"从 README 文字变成可下载的实物。
    """
    _ui_auth(request)
    data = _ui_dash.build_audit_zip()
    filename = "ue5_audit_" + time.strftime("%Y%m%d_%H%M%S") + ".zip"
    return Response(
        data, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/ui/api/screenshot")
def ui_api_screenshot(request: Request = None, name: str = ""):
    """画面面板数据源：<工程>/Saved/Screenshots/ue5-automation/ 最新 PNG。

    name 参数（v2 对比视图）：取指定文件（必须落在约定目录内，防路径逃逸）；
    缺省取最新一张。截图闭环就绪前目录不存在 → 404（页面显示"等待截图闭环"
    占位，规格书要求读不到不报错）。
    """
    _ui_auth(request)
    if name:
        base = os.path.abspath(_ui_dash._screenshots_dir() or "")
        cand = os.path.abspath(os.path.join(base, os.path.basename(name)))
        if not base or not cand.startswith(base) or not os.path.isfile(cand):
            return JSONResponse(status_code=404, content={
                "found": False, "detail": f"截图不存在: {os.path.basename(name)}"})
        return FileResponse(cand, media_type="image/png")
    path = _ui_dash.latest_screenshot()
    if not path:
        return JSONResponse(status_code=404, content={
            "found": False,
            "detail": "等待截图闭环：<工程>/Saved/Screenshots/ue5-automation/ 暂无截图"})
    return FileResponse(path, media_type="image/png")


def _scenario_results_dir() -> str:
    """场景 JUnit 目录（项目 Saved 优先；解析失败回退用户目录）。"""
    try:
        saved = _gt_run_handler(
            lambda: unreal.Paths.convert_relative_path_to_full(
                unreal.Paths.project_saved_dir()), timeout=2.0)
        return os.path.join(str(saved), "ue5-automation", "scenarios")
    except Exception:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ue5_bridge", "scenarios")


@app.get("/ui/api/scenarios")
def ui_api_scenarios(request: Request = None, limit: int = 20):
    """验收场景结果（只读）：读 JUnit XML 汇总 name/tests/failures/time。"""
    _ui_auth(request)
    import xml.etree.ElementTree as ET
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    d = _scenario_results_dir()
    items = []
    try:
        names = sorted((f for f in os.listdir(d) if f.endswith(".xml")),
                       key=lambda f: os.path.getmtime(os.path.join(d, f)),
                       reverse=True)[:limit]
    except OSError:
        names = []
    for fn in names:
        path = os.path.join(d, fn)
        try:
            st = os.stat(path)
            root = ET.parse(path).getroot()
            items.append({
                "file": fn,
                "name": root.get("name") or fn.rsplit("-", 1)[0],
                "tests": int(root.get("tests") or 0),
                "failures": int(root.get("failures") or 0),
                "time": float(root.get("time") or 0),
                "mtime_iso": time.strftime("%Y-%m-%dT%H:%M:%S",
                                           time.localtime(st.st_mtime)),
            })
        except Exception:
            items.append({"file": fn, "name": fn, "tests": 0, "failures": 1,
                          "time": 0, "mtime_iso": ""})
    return {"items": items, "dir": d}


@app.get("/ui/api/screenshots")
def ui_api_screenshots(request: Request = None, limit: int = 24):
    """对比视图胶片条：截图目录清单（新→旧，path/name/mtime/size）。"""
    _ui_auth(request)
    try:
        limit = max(1, min(int(limit), 48))
    except (TypeError, ValueError):
        limit = 24
    items = _ui_dash.screenshots_list(limit=limit)
    return {"items": items, "dir": _ui_dash._screenshots_dir() or ""}


@app.get("/ui/api/task")
def ui_api_task(request: Request = None):
    """任务脉络数据源：进行中任务（意图/计划/进度）+ 历史复盘 + 撤销状态。"""
    _ui_auth(request)
    if not _TASK_JOURNAL_AVAILABLE:
        return JSONResponse(status_code=503, content={
            "detail": "任务日志不可用（task_journal 导入失败）"})
    data = _task_journal.task_list()
    # 任务级撤销状态（T-20261027-TASK-UNDO）：只读展示，面板不提供写入口
    data["undo"] = _undo_status_cached()
    data["locks"] = _locks_snapshot()
    return data


@app.get("/ui/api/mcp-config")
def ui_api_mcp_config(request: Request = None):
    """/ui 接入配置（T-20261027-ONBOARD）：生成主流 AI 客户端的 MCP 配置片段。

    只读：只吐文本，不写任何文件、不改任何设置。token 来自当前进程（本机）。
    """
    _ui_auth(request)
    port = int(os.environ.get("UE5_BRIDGE_PORT", "8889"))
    token = _BRIDGE_TOKEN or ""
    http_url = "http://127.0.0.1:%d/mcp" % port
    py = sys.executable or "python"
    # 编辑器内 sys.executable 是 UnrealEditor.exe（不是 Python 解释器）——
    # 推到引擎自带 python.exe，否则 stdio 配置粘出去必然失败
    try:
        if str(py).lower().endswith("unrealeditor.exe"):
            _bin = os.path.dirname(str(py))                 # .../Engine/Binaries/Win64
            _cand = os.path.join(os.path.dirname(_bin), "ThirdParty", "Python3",
                                 "Win64", "python.exe")
            if os.path.isfile(_cand):
                py = _cand
    except Exception:
        pass
    # ⚠️ unreal.Paths 是 UE API：HTTP 线程直呼在 5.8 会被拒（GT 纪律）——
    # 之前这里静默降级成 <项目> 占位符，粘出去的配置根本跑不起来。
    try:
        def _content_dir():
            return unreal.Paths.convert_relative_path_to_full(
                unreal.Paths.project_content_dir())

        content_py = _gt_run_handler(_content_dir, timeout=5.0)
        stdio_script = os.path.normpath(os.path.join(
            str(content_py), "Python", "ue5_mcp_stdio.py"))
    except Exception:
        stdio_script = "<项目>/Content/Python/ue5_mcp_stdio.py"

    http_cfg = {
        "mcpServers": {
            "ue5-automation": {
                "type": "http",
                "url": http_url,
                "headers": {"X-Skill-Token": token},
            }
        }
    }
    stdio_cfg = {
        "mcpServers": {
            "ue5-automation": {
                "command": py,
                "args": [stdio_script],
                "env": {"UE5_BRIDGE_TOKEN": token,
                        "UE5_BRIDGE_PORT": str(port)},
            }
        }
    }
    return {
        "http_url": http_url,
        "token": token,
        "configs": {
            "http": json.dumps(http_cfg, ensure_ascii=False, indent=2),
            "stdio": json.dumps(stdio_cfg, ensure_ascii=False, indent=2),
        },
        "notes": [
            "支持 Streamable HTTP 的客户端（ZCode / 新版 Claude Desktop / Cursor）用 http 配置",
            "只支持 stdio 的客户端用 stdio 配置（需本机有 Python，指向 Content/Python/ue5_mcp_stdio.py）",
            "token 是本机随机值，勿外传；换机器会在 %APPDATA%/ue5_bridge/token 重新生成",
        ],
    }


@app.get("/ui/api/stats")
def ui_api_stats(request: Request = None):
    """统计图表数据源：按工具/分类聚合调用数、错误数、均耗时。"""
    _ui_auth(request)
    if not _DASHBOARD_AVAILABLE:
        return JSONResponse(status_code=503, content={"detail": "统计管道不可用"})
    return _ui_dash.stats_summary()


# ── FastAPI 生命周期已迁移至文件头部 lifespan（T-20260908-DEPRECATION-FIX）──
# 原 @app.on_event("startup") on_startup / @app.on_event("shutdown") on_shutdown
# 已等价替换为头部 lifespan 上下文管理器，消除 DeprecationWarning，行为不变。


def start(port: int = None, host: str = "127.0.0.1"):
    """启动 Bridge 服务器（在 UE Python Console 中调用）。

    🔒 B-003 修复：默认端口从环境变量 UE5_BRIDGE_PORT 读取，回退 8889。

    用法：
        import ue5_bridge
        ue5_bridge.start()

    Args:
        port: HTTP 监听端口，默认从环境变量 UE5_BRIDGE_PORT 读取（回退 8889）
        host: 监听地址，默认 127.0.0.1
    """
    global _server_thread, _uvicorn_server
    if _server_running:
        print("[ue5_bridge] [WARN] 服务已在运行")
        return

    # 🔧 T-20260806-ATEXIT-UEVERIFY Bug①：默认端口解析提前到预检之前
    #   旧代码 port=None 时预检 _s.connect((host, None)) 抛 TypeError 被 except 吞掉
    #   → 预检静默失效（不检查默认端口冲突）。
    #   修复：先解析默认端口再预检 → port=None 不再吞错，且「未配置（默认 8889）」
    #         与「冲突」都能被正确预检。
    if port is None:
        port = int(os.environ.get("UE5_BRIDGE_PORT", "8889"))

    # 🔧 T-20260805-STOP-FIX：检测端口是否被旧 server 占（reload 后旧 server 还活着）
    # 若端口已占 → 提示用户先用 stop()（或重启 UE）才能 reload
    import socket as _socket
    _s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    _s.settimeout(0.5)
    try:
        _s.connect((host, port))
        _s.close()
        print(f"[ue5_bridge] [WARN] 端口 {port} 已被旧 server 占用 — 请先 stop() 或重启 UE 编辑器")
        print(f"[ue5_bridge] [WARN] 修复方法：在 UE Python Console 执行 stop() + start()（需停掉旧 server）")
        return
    except Exception:
        pass  # 端口空闲，正常启动
    finally:
        try: _s.close()
        except: pass

    # v0.5 安全修复：禁止非 localhost 绑定
    if host not in ("127.0.0.1", "localhost"):
        print(f"[ue5_bridge] [SEC] 安全限制：仅允许 127.0.0.1 绑定，拒绝 host={host}")
        return

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",        # 减少 UE 输出日志噪音
        timeout_keep_alive=120,     # C-003 修复：UE 内 Python 操作可能 > 30s
    )
    # 🔧 T-20260723-QUEUEFIX：启动持久 GameThread tick worker
    _gt_start_worker()

    # 画面面板数据源注入（start() 由 init_unreal 在 GameThread 上调 —— unreal.Paths
    # 只能 GT 调，HTTP 线程连 get_engine_version 都抛异常；真机实录 2026-10-24）。
    # 注入失败不阻断启动：面板退回"暂无截图"占位（fail-open，规格书口径）。
    try:
        if _ui_dash is not None:
            _saved_dir = unreal.Paths.convert_relative_path_to_full(
                unreal.Paths.project_saved_dir())
            _ui_dash.set_screenshots_dir(
                os.path.join(str(_saved_dir), "Screenshots", "ue5-automation"))
    except Exception as _shot_dir_err:
        print(f"[ue5_bridge] [WARN] 画面面板目录注入失败（面板显示占位）: {_shot_dir_err}")

    server = uvicorn.Server(config)
    _uvicorn_server = server  # 🔧 保存供 stop() 调用

    def _run():
        try:
            server.run()
        except Exception as e:
            print(f"[ue5_bridge] [FAIL] 服务异常: {e}")

    # 🔧 T-20260805-CRASH-FIX：daemon=False + atexit.register(stop)
    #   根因：daemon=True 时 UE 关闭时 Python 解释器直接强杀 uvicorn 线程，
    #         正在写的 asyncio 栈被撕 → 写地址 0x... 越界崩溃
    #   修复：daemon=False 让 Python 退出前等 server 线程；atexit 触发 stop()
    #         → 优雅关闭释放端口 → 然后 UE 安全退出
    _server_thread = threading.Thread(target=_run, daemon=False)
    _server_thread.start()

    # 🔧 注册退出钩子（首次启动时注册，重复 start 也不会重复注册）
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(stop)
        _atexit_registered = True
        print(f"[ue5_bridge] [OK] atexit 钩子已注册（UE 关闭时自动 stop）")

    print(f"[ue5_bridge] [OK] 服务已在后台启动 → http://{host}:{port}/docs")


def stop():
    """停止 Bridge 服务器。

    🔧 T-20260805-STOP-FIX 修复：
        旧版只改 _server_running flag，但 uvicorn server 没真停，导致 reload 模块后
        端口冲突 + 旧 server 用旧 token 验证失败。修复：调用 uvicorn.Server.shutdown()
        优雅停止，释放端口（最多等 2 秒）。
    🔧 T-20260805-CRASH-ATEXIT-FIX P0-①③ 修复（本派单）：
        P0-①：stop() 未调 _gt_stop_worker() → tick worker 句柄泄漏 + UE 关闭时
               Slate 残留回调访问半卸载 unreal → use-after-free AV。
        P0-③：atexit → stop() 期间 uvicorn 并发 → _gt_worker 仍从队列取任务执行
               → 访问半卸载 unreal → AV（核心崩溃路径）。
        修复顺序：
          ① 先 _gt_stop_worker()（注销 Slate 回调 + 置 STOPPED 标记）
          ② _drain_gt_queue()（丢弃残留任务、唤醒等待方）
          ③ 再优雅停 uvicorn + join 线程
          ④ 函数体顶层 try/except——atexit 回调中异常禁止逃逸
             （逃逸 → UE 关闭时解释器崩溃）
    """
    global _server_running, _server_thread, _uvicorn_server
    if not _server_running:
        print("[ue5_bridge] 服务未在运行")
        return

    try:
        # 0. 先停 GameThread tick worker（P0-①③：注销 Slate 回调，防残留回调执行）
        try:
            _gt_stop_worker()
        except Exception as _e:
            print(f"[ue5_bridge] [WARN] 停止 GameThread worker 失败: {_e}")

        # 1. 排空队列（P0-③：shutdown 期间不再执行任何残留任务）
        try:
            _drain_gt_queue()
        except Exception as _e:
            print(f"[ue5_bridge] [WARN] 排空 GameThread 队列失败: {_e}")

        # 2. 通知 uvicorn 优雅关闭
        if _uvicorn_server is not None:
            try:
                _uvicorn_server.should_exit = True
                # 不调 force_exit，等待 graceful shutdown 释放端口
            except Exception as e:
                print(f"[ue5_bridge] [WARN] uvicorn.shutdown 失败: {e}")

        # 3. 等待线程退出（最多 2 秒）
        if _server_thread is not None and _server_thread.is_alive():
            _server_thread.join(timeout=2.0)
    except Exception as _e:
        # 🔧 P0-③：atexit 回调中任何异常都不能逃逸（逃逸 → UE 关闭时解释器崩溃）
        print(f"[ue5_bridge] [WARN] stop() 异常（已吞掉，防止 atexit 崩溃）: {_e}")
        traceback.print_exc()
    finally:
        _server_running = False
        _server_thread = None
        _uvicorn_server = None
        print("[ue5_bridge] 服务已停止")


def status() -> Dict:
    """返回 Bridge 运行状态。"""
    return {
        "running": _server_running,
        "bridge_loaded": BRIDGE_AVAILABLE,
        "port": 8889,
        "uptime_seconds": int(time.time() - _start_time),
        "request_count": _request_count,
        "ue_info": _get_ue_info(),
    }


# ══════════════════════════════════════════════════════════
#  自测 — 当脚本直接运行时执行
# ══════════════════════════════════════════════════════════

if _TEST_MODE:
    # TEST MODE: auto-start GameThread tick worker (fake env)
    try:
        _gt_start_worker()
    except Exception as _e:
        print(f"[ue5_bridge] WARN: test-mode auto-start tick worker failed: {_e}")


if __name__ == "__main__":
    print("=" * 60)
    print("  ue5_bridge.py 自测")
    print("=" * 60)

    # 1. 检查 unreal 模块
    print(f"\n✅ unreal 模块: {unreal.__name__}")

    # 2. 检查 Bridge DLL
    if BRIDGE_AVAILABLE:
        print(f"✅ BlueprintPythonBridge: 已加载")
        apis = [
            "create_actor_blueprint", "compile_blueprint",
            "get_event_graph", "list_nodes", "list_pins",
            "connect_pins", "get_node_connections", "get_pin_infos",
        ]
        for api in apis:
            has = hasattr(bridge, api)
            print(f"  {'✅' if has else '❌'} {api}")
    else:
        print("⚠️ BlueprintPythonBridge: 未加载（L2 不可用）")

    # 3. UE 信息
    ue_info = _get_ue_info()
    for k, v in ue_info.items():
        print(f"  {k}: {v}")

    # 4. 启动
    print(f"\n启动服务 → http://127.0.0.1:8889/docs")
    start()

    print(f"\n{'=' * 60}")
    print("  自测完毕。按 Ctrl+C 停止。")
    print(f"{'=' * 60}")

    try:
        while _server_running:
            time.sleep(1)
    except KeyboardInterrupt:
        stop()
        print("\n[ue5_bridge] 用户中断")
