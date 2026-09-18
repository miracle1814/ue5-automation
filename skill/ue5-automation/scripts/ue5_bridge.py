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
import traceback
import threading
import atexit  # 🔧 T-20260805-CRASH-FIX: UE 关闭时自动 stop 服务
import tempfile
import uuid
import secrets  # T-20260908-CPPAPIS-AUDIT-FIX P2-2: compare_digest 恒定时间比较
import queue as _queue
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel

# ── FastAPI ──────────────────────────────────────────────
try:
    from fastapi import FastAPI, HTTPException, Request, Body
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
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

# ── trace_id 中间件（T-20260806-UE5BRIDGE-CRASH-FIX 方案E · 执剑者补充）──
# 目标：每个请求分配 trace_id（X-Trace-Id 头或随机 8 位 hex），贯穿日志/响应头，
#       崩溃复发时按 trace_id 区分触发路径（队列路径 vs execute_python_on_game_thread 路径②）。
# 注意：middleware 保持 async 是 uvicorn 推荐用法，不影响端点改为同步 def（线程池）。
@app.middleware("http")
async def add_trace_id(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id", uuid.uuid4().hex[:8])
    request.state.trace_id = trace_id
    response = await call_next(request)
    response.headers["X-Trace-Id"] = trace_id
    return response

# ── Token 认证（A-004 修复：原零认证 / v0.5 修复：消除硬编码 test123）──
# v2.20 修复（E-11 冷启动竞态）：旧实现在模块导入时**无条件**重新生成随机 token
#   并覆盖 token 文件 → 冷启动序列「UE 已开但 bridge 未启 → 宿主 runner 先读
#   token 文件（旧值）→ ensure_http_bridge 启动 bridge（生成新值覆盖文件）→
#   宿主用旧值轮询 /health → 恒 403 → 误报『bridge 启动后无响应』」。
#   现在：token 文件已存在（非空）→ **复用**；只有文件缺失/为空/不可读时才生成。
#   UE5_BRIDGE_TOKEN 环境变量仍然最高优先。
_BRIDGE_TOKEN = os.environ.get("UE5_BRIDGE_TOKEN")

if not _BRIDGE_TOKEN:
    _token_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "ue5_bridge")
    _token_file = os.path.join(_token_dir, "token")
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
        result = fn(*args, **kwargs)
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


def _gt_worker(delta_time: float):
    """持久的 GameThread tick worker——每帧从队列取一个任务执行。"""
    global _GT_WORKER_STOPPED
    # 🔧 T-20260805-CRASH-ATEXIT-FIX P0-③：已停止（stop()/UE 关闭）后，即使
    #   unregister 失败导致 Slate 残留回调被调用，也立即跳过——不取任务、不碰
    #   任何 unreal 对象（防 use-after-free AV）。
    if _GT_WORKER_STOPPED:
        return True  # 已停止：直接空转，不再执行任何队列任务
    try:
        task_id, fn, args, kwargs = _GT_QUEUE.get_nowait()
    except _queue.Empty:
        return True  # 继续等待下一帧

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
    """获取 UE 编辑器和项目信息。"""
    info = {}
    try:
        info["engine_version"] = str(unreal.SystemLibrary.get_engine_version())
    except:
        info["engine_version"] = "unknown"
    try:
        info["project_name"] = unreal.SystemLibrary.get_game_name()
    except:
        info["project_name"] = "unknown"
    try:
        info["project_dir"] = unreal.Paths.project_dir()
    except:
        info["project_dir"] = "unknown"
    try:
        info["bridge_loaded"] = BRIDGE_AVAILABLE
    except:
        info["bridge_loaded"] = False
    return info


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


def _cmd_compile_blueprint(bp_path: str) -> dict:
    """编译蓝图（v0.6 P1：编译后扫描日志匹配错误模式）。"""
    bp = _resolve_blueprint(bp_path)
    compile_ok = False
    compile_error = None
    try:
        if BRIDGE_AVAILABLE:
            compile_ok = _safe_call(bridge.compile_blueprint, bp, error_cat="compile")
        else:
            compile_ok = _safe_call(
                unreal.BlueprintEditorLibrary.compile_blueprint, bp,
                error_cat="compile"
            )
    except Exception as e:
        compile_error = str(e)

    # ── v0.6 P1：编译后扫描 UE 日志匹配错误模式 ──
    log_matches = []
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
                tail = lines[-200:] if len(lines) > 200 else lines

            # 加载 error_patterns 匹配
            _ep_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'config', 'error_patterns.json'
            )
            # 兜底：UE 内 __file__ 可能不可靠 → 用 COPAW_WORKSPACE
            if not os.path.exists(_ep_path):
                _ws = os.environ.get("COPAW_WORKSPACE",
                    os.path.join(os.path.expanduser("~"), ".copaw", "workspaces", "default"))
                _ep_path = os.path.join(_ws, "skills", "ue5-automation", "config", "error_patterns.json")
            if os.path.exists(_ep_path):
                with open(_ep_path, 'r', encoding='utf-8') as epf:
                    _ep_data = json.load(epf)
                import re
                for ptn in _ep_data.get('patterns', []):
                    regex = ptn.get('pattern', {}).get('log_regex', '')
                    if not regex or regex == '.*':
                        continue  # 跳过通配符（无法精确匹配）
                    try:
                        _re = re.compile(regex, re.IGNORECASE)
                        for line in tail:
                            m = _re.search(line)
                            if m:
                                log_matches.append({
                                    'pattern_id': ptn['id'],
                                    'title': ptn['title'],
                                    'severity': ptn['severity'],
                                    'matched_line': line.strip()[:200],
                                    'fix_action': ptn.get('fix_action', ''),
                                })
                                break  # 每个模式只记录首次命中
                    except re.error:
                        pass
    except Exception:
        pass  # 日志读取失败不影响主流程

    result = {"compiled": bool(compile_ok)}
    # D-2（T-20260913-UE5BRIDGE-DELETE-COMPILE-FIX）：编译后**强制回读**
    result["compile_status"] = _bp_status_str(
        bp, compiled=bool(compile_ok))
    if compile_error:
        result["error"] = compile_error
    if log_matches:
        result["log_matches"] = log_matches
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
    out = _delete_asset_cascade(asset_path, max_attempts)
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
        bridge.execute_python_on_game_thread(code)
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
            res, used, tried = _ue_call_any(("editor_play_in_editor",))
        else:
            res, used, tried = _ue_call_any(("editor_play_simulate",))
        if used == "":
            raise BridgeError(
                "PIE 启动 API 不可用（跨库试过: %s）" % ",".join(tried),
                category="pie",
                suggestion="UE5.1 实测：editor_play_simulate 在 EditorLevelLibrary 与 "
                           "LevelEditorSubsystem 均可；editor_play_in_editor 仅子系统有")
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
        rot = u.Rotator(float(rot3[0]), float(rot3[1]), float(rot3[2]))
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
            rot = u.Rotator(float(rot3[0]), float(rot3[1]), float(rot3[2]))
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

def _resolve_log_file(log_file: str = ""):
    """定位 UE 日志文件（显式 > project_log_dir > project/Saved/Logs 推导）。"""
    if log_file:
        p = os.path.abspath(log_file)
        return p if os.path.isfile(p) else None
    u = globals().get("unreal")
    dirs = []
    if u is not None:
        pdet = getattr(u, "Paths", None)
        for n in ("project_log_dir",):
            fn = getattr(pdet, n, None) if pdet is not None else None
            if callable(fn):
                try:
                    dirs.append(str(fn()))
                except Exception:
                    pass
        fn = getattr(pdet, "project_dir", None) if pdet is not None else None
        if callable(fn):
            try:
                dirs.append(os.path.join(str(fn()), "Saved", "Logs"))
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
            r = _COMMAND_WHITELIST[cmd](**params)
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
    mat = unreal.EditorAssetLibrary.load_asset(mat_path)
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


def _cmd_material_create(material_path: str, package_path: str = "") -> dict:
    """创建材质资产（UMaterial + MaterialFactoryNew）。"""
    _pywrap_require_str("material_path", material_path)
    path = str(material_path).strip()
    if not path.startswith("/"):
        raise BridgeError("material_path 需为 /Game/... 包路径", category="param_type")
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
        expr = mel.create_material_expression(mat, cls, int(pos[0]), int(pos[1]))
        if expr is None:
            raise BridgeError("create_material_expression 返回 None（表达式未创建）",
                              category="material")
        return {"name": str(expr.get_name()), "class": str(cls.get_path_name())}

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
        ok = mel.connect_material_expressions(
            src, str(from_output or ""), dst, str(to_input or ""))
        if not ok:
            raise BridgeError(
                "connect_material_expressions 返回 False（%s.%s → %s.%s）—— "
                "检查引脚名（空串=默认第一引脚）与类型兼容性"
                % (from_name, from_output or "<default>", to_name, to_input or "<default>"),
                category="material")
        return {"connected": True, "from": from_name, "to": to_name}

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
        return {"connected": True, "from": from_name, "property": property_name}

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
        if ok is False:
            raise BridgeError("recompile_material 返回 False（材质编译失败——检查连接/表达式）",
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
    p = str(widget_path or "").strip()
    if not p.startswith("/Game"):
        raise BridgeError("widget_path 需为 /Game/... 包路径", category="param_type")
    return p.split(".")[0]


def _cmd_widget_create(widget_path: str, package_path: str = "") -> dict:
    """创建 UMG 控件蓝图（父类 UUserWidget；自动建 WidgetTree + 编译）。"""
    _pywrap_require_str("widget_path", widget_path)
    b = _wrapb_require()
    full = _wrapb_norm_path(widget_path)
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
        return {"widget": full, "name": str(widget_name),
                "class": str(widget_class), "parent": str(parent_name or "<root>")}

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


# ── 白名单命令表 ──
_COMMAND_WHITELIST = {
    "health":            lambda **kw: _get_ue_info(),
    "list_assets":       lambda path="/Game/", recursive=True, **kw: _cmd_list_assets(path, recursive),
    "read_blueprint":    lambda bp_path, level="L1", include_cdo=True, include_conn=True, **kw: _cmd_read_blueprint(bp_path, level, include_cdo, include_conn),
    "read_variables":    lambda bp_path, **kw: _cmd_read_variables(bp_path),
    "read_cdo":          lambda bp_path, **kw: _cmd_read_cdo(bp_path),
    "read_nodes":        lambda bp_path, graph_name=None, **kw: _cmd_read_nodes(bp_path, graph_name),
    "read_connections":  lambda bp_path, graph_name="EventGraph", **kw: _cmd_read_connections(bp_path, graph_name),
    "build_blueprint":   lambda bp_name, pkg="/Game/Blueprints/", parent="Actor", vars=None, comps=None, interfaces=None, compile_after=True, **kw: _cmd_build_blueprint(bp_name, pkg, parent, vars, comps, interfaces, compile_after),  # 兼容旧调用，新代码用 POST /build
    "add_variable":      lambda bp_path, var_name, var_type, default_value="", category="", expose=True, **kw: _cmd_add_variable(bp_path, var_name, var_type, default_value, category, expose),
    "add_component":     lambda bp_path, comp_name, comp_class, properties=None, **kw: _cmd_add_component(bp_path, comp_name, comp_class, properties),
    "add_function_node": lambda bp_path, graph_name, function_path, node_pos=None, **kw: _cmd_add_function_node(bp_path, graph_name, function_path, node_pos),
    "connect_pins":      lambda bp_path, connections, **kw: _cmd_connect_pins(bp_path, connections),
    "compile_blueprint": lambda bp_path, **kw: _cmd_compile_blueprint(bp_path),
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
    # ② Actor 基础族（world="editor"|"pie"；PIE 中即运行时对象）
    "find_actors":       lambda actor_class="", name_filter="", world="editor", limit=100, **kw: _cmd_find_actors(actor_class, name_filter, world, limit),
    "spawn_actor":       lambda actor_class, location=None, rotation=None, world="editor", mobility="", **kw: _cmd_spawn_actor(actor_class, location, rotation, world, mobility),
    "set_actor_transform": lambda actor_name, location=None, rotation=None, world="editor", **kw: _cmd_set_actor_transform(actor_name, location, rotation, world),
    "get_actor_properties": lambda actor_name, property_name="", world="editor", limit=200, **kw: _cmd_get_actor_properties(actor_name, property_name, world, limit),
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


@app.post("/command", response_model=CommandResponse)
def execute_command(req: CommandRequest, request: Request = None):  # 🔧 B: async→sync def（T-20260806-UE5BRIDGE-CRASH-FIX 方案B·线程池隔离）
    """白名单命令模式 — 客户端只能说预定义命令。

    🔒 A-003 + A-004 安全升级：
      - 替代旧 /execute 沙箱端点
      - 只允许白名单命令，杜绝任意代码执行
      - 需 X-Skill-Token 头认证

    可用命令（72 条 = 原 15 + T-20260910-UE5BRIDGE-PYWRAP-L1-IMPL 新增 11 + T-20260912-UE5BRIDGE-PYWRAP-L2-IMPL 新增 21 + T-20260913-UE5BRIDGE-SAVE-ASSET 新增 1 + T-20260913-UE5BRIDGE-DELETE-COMPILE-FIX 新增 1 + T-20260914-UE5BRIDGE-BUILD-HOLD-FIX 新增 1 + T-20260918-V3-MCP-FUSION 新增 9 + T-20260918-V3-MAT 新增 9 + T-20260918-V3-UMG 新增 4）:
        health, list_assets, read_blueprint, read_variables, read_cdo,
        read_nodes, read_connections, build_blueprint(兼容), add_variable,
        add_component, add_function_node, connect_pins, compile_blueprint,
        set_pin_default_value, get_pin_default_value

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
            set_actor_transform · get_actor_properties
        日志查询：logs_read（游标增量 + category/pattern 过滤）
        批量执行：editor_batch（白名单命令序列单次请求；fail-loud 可中止）
        材质族（v3.0）：material_create · material_add_expression ·
            material_connect_expressions · material_connect_property ·
            material_set_expression_property · material_compile · material_read ·
            material_instance_create · material_instance_set_parameter
        UMG 控件族（v3.0，需 v3.0 版 DLL）：widget_create · widget_add_child ·
            widget_set_property · widget_read_tree

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
        result = _COMMAND_WHITELIST[req.command](**req.params)
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
            bridge.execute_python_on_game_thread(code)
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
                bridge.execute_python_on_game_thread(gt_code)

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

        if BRIDGE_AVAILABLE and hasattr(bridge, 'execute_python_on_game_thread'):
            # 🔧 E: 路径② trace_id 标记（T-20260806-UE5BRIDGE-CRASH-FIX 方案E）
            _OP_LOG.info(f"[trace_id={getattr(request.state, 'trace_id', '?')}] PATH2 execute_python_on_game_thread(connect_pins)")
            bridge.execute_python_on_game_thread(gt_code)

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
                result["success"] = gt_result.get("success", True)
                result["results"] = gt_result.get("results", [])
                if gt_result.get("error"):
                    result["error"] = gt_result["error"]
            else:
                result["success"] = False
                result["error"] = f"GameThread 执行未产出结果文件（超时 {_timeout}s）"
        else:
            result["success"] = False
            result["error"] = "Bridge DLL 不可用或缺少 execute_python_on_game_thread；无法连线"

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
                    bridge.execute_python_on_game_thread(_gt_code)
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
#  · 工具面：约 23 个高频工具 + `ue5_command` 白名单透传（描述含全量命令名），
#    避免把 59 条命令平铺撑爆 tools/list 的客户端 token 预算。
#  · 复用：需要端点级语义（/build /connect 等）的工具体通过 _TrustedRequest
#    垫片调用既有端点函数 —— MCP 层已完成等价鉴权，零逻辑复制。
# ══════════════════════════════════════════════════════════

_MCP_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
_MCP_SERVER_INFO = {"name": "ue5-automation-bridge", "version": "3.0.0"}


class _TrustedRequest:
    """内部可信请求垫片：让既有端点函数的 _verify_token / trace_id 读取通过。

    仅在 /mcp 已完成等价 token 校验后使用（同一进程、同一 token）。
    """

    class _Headers:
        def get(self, _self, key, default=None):
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


def _mcp_tool_defs():
    """MCP tools/list 清单（请求时构造 —— ue5_command 描述含动态白名单）。"""
    S = {"type": "string"}
    B = {"type": "boolean"}
    I = {"type": "integer"}
    N3 = {"type": "array", "items": {"type": "number"},
          "description": "[x, y, z]"}
    TOOLS = [
        {"name": "ue5_health",
         "description": "Bridge/UE 健康检查：引擎版本、项目名、bridge_apis 清单",
         "inputSchema": _mcp_obj({})},
        {"name": "ue5_diag",
         "description": "Bridge 诊断：全部 C++ API 方法矩阵 + 关键 API 缺失项",
         "inputSchema": _mcp_obj({})},
        {"name": "ue5_list_assets",
         "description": "列出 /Game 资产路径",
         "inputSchema": _mcp_obj({"path": S, "recursive": B}, ["path"])},
        {"name": "ue5_read_blueprint",
         "description": "读取蓝图（L1 类信息+变量+CDO / L2 节点+连线 / full）",
         "inputSchema": _mcp_obj(
             {"blueprint_path": S, "level": S, "include_cdo": B,
              "include_connections": B}, ["blueprint_path"])},
        {"name": "ue5_read_nodes",
         "description": "读取蓝图节点（不传 graph_name = 全图）",
         "inputSchema": _mcp_obj({"bp_path": S, "graph_name": S}, ["bp_path"])},
        {"name": "ue5_read_connections",
         "description": "读取蓝图连线拓扑",
         "inputSchema": _mcp_obj({"bp_path": S, "graph_name": S}, ["bp_path"])},
        {"name": "ue5_build_blueprint",
         "description": "创建蓝图（资产+变量+组件+接口+事件+函数节点+连线+编译）。"
                        "spec 字段：blueprint_name(或 name), package_path, parent_class, "
                        "variables[{name,type,default_value,category}], "
                        "components[{name,component_class,properties}], interfaces[str], "
                        "events[{event_name|name,pos}], "
                        "functions[{function_name|name, target_class, function_path, "
                        "defaults:{pin:val}, pos}], "
                        "connections[{src_node,src_pin,dst_node,dst_pin}], compile_after。"
                        "节点引用可用 32 位 GUID（推荐）或节点标题。",
         "inputSchema": _mcp_obj({"spec": {"type": "object"}}, ["spec"])},
        {"name": "ue5_build_batch",
         "description": "批量创建蓝图（原子化：任一失败回滚已建资产）。"
                        "{blueprints: [spec, ...]}",
         "inputSchema": _mcp_obj({"blueprints": {"type": "array"}}, ["blueprints"])},
        {"name": "ue5_compile",
         "description": "编译蓝图 + 回读编译状态 + 日志错误模式扫描",
         "inputSchema": _mcp_obj({"bp_path": S}, ["bp_path"])},
        {"name": "ue5_connect_pins",
         "description": "引脚连线（三重拓扑验证）。src_node/dst_node 用节点标题或 "
                        "32 位 GUID；同名节点必须 GUID（标题歧义会 fail-loud 拒绝）",
         "inputSchema": _mcp_obj(
             {"blueprint_path": S, "connections": {"type": "array"}},
             ["blueprint_path", "connections"])},
        {"name": "ue5_disconnect_pin",
         "description": "断开节点某引脚的全部连线",
         "inputSchema": _mcp_obj(
             {"blueprint_path": S, "node_id": S, "pin_name": S},
             ["blueprint_path", "node_id", "pin_name"])},
        {"name": "ue5_delete_node",
         "description": "删除节点（node_id = 标题或 GUID；dry_run 仅检查引用）",
         "inputSchema": _mcp_obj(
             {"blueprint_path": S, "node_id": S, "dry_run": B},
             ["blueprint_path", "node_id"])},
        {"name": "ue5_save_asset",
         "description": "保存资产落盘（only_if_is_dirty=true 仅脏包写盘）",
         "inputSchema": _mcp_obj({"asset_path": S, "only_if_is_dirty": B},
                                  ["asset_path"])},
        {"name": "ue5_delete_asset",
         "description": "级联删除资产（删后回读验证；保存作用域=引用者包，绝不全工程保存）",
         "inputSchema": _mcp_obj({"asset_path": S, "max_attempts": I},
                                  ["asset_path"])},
        {"name": "ue5_pie_start",
         "description": "启动 PIE/Simulate（异步；用 ue5_pie_state 轮询确认）",
         "inputSchema": _mcp_obj({"mode": S})},
        {"name": "ue5_pie_stop",
         "description": "结束 PIE/Simulate",
         "inputSchema": _mcp_obj({})},
        {"name": "ue5_pie_state",
         "description": "PIE 状态：is_playing / 世界名 / 暂停 / 游戏时间",
         "inputSchema": _mcp_obj({})},
        {"name": "ue5_pie_actors",
         "description": "枚举 PIE 世界 Actor（运行时对象，路径含 UEDPIE_0_）",
         "inputSchema": _mcp_obj({"actor_class": S, "name_filter": S, "limit": I})},
        {"name": "ue5_pie_get_property",
         "description": "读 PIE 中 Actor 属性（property_name 空 = 列候选属性名）",
         "inputSchema": _mcp_obj({"actor_name": S, "property_name": S},
                                  ["actor_name"])},
        {"name": "ue5_pie_set_transform",
         "description": "运行时传送 PIE Actor（location/rotation 至少一个）",
         "inputSchema": _mcp_obj(
             {"actor_name": S, "location": N3, "rotation": N3}, ["actor_name"])},
        {"name": "ue5_logs",
         "description": "读 UE 日志（cursor=0 取尾部；cursor>0 增量取新增；"
                        "category/pattern 过滤）",
         "inputSchema": _mcp_obj({"lines": I, "category": S, "pattern": S,
                                  "cursor": I})},
        {"name": "ue5_batch",
         "description": "白名单命令批量执行（单次请求多步操作；任一步失败默认中止）",
         "inputSchema": _mcp_obj(
             {"steps": {"type": "array"}, "stop_on_error": B}, ["steps"])},
        {"name": "ue5_command",
         "description": ("白名单命令透传（59 条全量能力入口）。command ∈ "
                         + ", ".join(sorted(_COMMAND_WHITELIST.keys()))
                         + "；params 为该命令入参对象。未登记命令 fail-loud 拒绝。"),
         "inputSchema": _mcp_obj({"command": S, "params": {"type": "object"}},
                                  ["command"])},
    ]
    return TOOLS


def _mcp_impl_command(args):
    cmd = str(args.get("command") or "").strip()
    params = args.get("params") or {}
    if cmd not in _COMMAND_WHITELIST:
        raise BridgeError("Unknown command: %s" % cmd, category="param_type",
                          suggestion="可用命令：%s" % ", ".join(sorted(_COMMAND_WHITELIST.keys())))
    r = _COMMAND_WHITELIST[cmd](**params)
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
    return build_blueprint(BuildRequest(**spec), _TrustedRequest())


def _mcp_impl_build_batch(args):
    bps = args.get("blueprints")
    if not isinstance(bps, list) or not bps:
        raise BridgeError("blueprints 必须为非空数组", category="param_type")
    return build_batch(BuildBatchRequest(blueprints=bps), _TrustedRequest())


def _mcp_impl_dispatch(name, args):
    """工具名 → 实现（未列出的名字返回 None）。"""
    if not isinstance(args, dict):
        args = {}
    simple = {
        "ue5_health": lambda a: _get_ue_info(),
        "ue5_diag": lambda a: bridge_diag(),
        "ue5_list_assets": lambda a: _cmd_list_assets(a.get("path", "/Game/"),
                                                      a.get("recursive", True)),
        "ue5_read_blueprint": lambda a: read_blueprint(
            ReadRequest(blueprint_path=a["blueprint_path"],
                        level=a.get("level", "L1"),
                        include_cdo=a.get("include_cdo", True),
                        include_connections=a.get("include_connections", True)),
            _TrustedRequest()),
        "ue5_read_nodes": lambda a: _cmd_read_nodes(a["bp_path"], a.get("graph_name")),
        "ue5_read_connections": lambda a: _cmd_read_connections(
            a["bp_path"], a.get("graph_name", "EventGraph")),
        "ue5_build_blueprint": _mcp_impl_build,
        "ue5_build_batch": _mcp_impl_build_batch,
        "ue5_compile": lambda a: _cmd_compile_blueprint(a["bp_path"]),
        "ue5_connect_pins": lambda a: connect_pins(
            ConnectRequest(blueprint_path=a["blueprint_path"],
                           connections=a.get("connections", [])),
            _TrustedRequest()),
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
        "ue5_pie_start": lambda a: _cmd_pie_start(a.get("mode", "simulate")),
        "ue5_pie_stop": lambda a: _cmd_pie_stop(),
        "ue5_pie_state": lambda a: _cmd_pie_state(),
        "ue5_pie_actors": lambda a: _cmd_find_actors(
            a.get("actor_class", ""), a.get("name_filter", ""), "pie",
            a.get("limit", 100)),
        "ue5_pie_get_property": lambda a: _cmd_get_actor_properties(
            a["actor_name"], a.get("property_name", ""), "pie", 200),
        "ue5_pie_set_transform": lambda a: _cmd_set_actor_transform(
            a["actor_name"], a.get("location"), a.get("rotation"), "pie"),
        "ue5_logs": lambda a: _cmd_logs_read(
            a.get("lines", 200), a.get("category", ""), a.get("pattern", ""),
            a.get("cursor", 0)),
        "ue5_batch": lambda a: _cmd_editor_batch(
            a["steps"], a.get("stop_on_error", True)),
        "ue5_command": _mcp_impl_command,
    }
    return simple.get(name)


def _mcp_err(_id, code, message):
    return {"jsonrpc": "2.0", "id": _id,
            "error": {"code": code, "message": message}}


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
                "result": {"tools": _mcp_tool_defs()}}, 200
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
            result = impl(args)
            is_err = (isinstance(result, dict)
                      and result.get("success") is False)
            return {"jsonrpc": "2.0", "id": _id,
                    "result": {"content": [{"type": "text",
                                            "text": _safe_json(result)}],
                               "isError": bool(is_err)}}, 200
        except Exception as e:
            return {"jsonrpc": "2.0", "id": _id,
                    "result": {"content": [{"type": "text",
                                            "text": "%s: %s" % (type(e).__name__, e)}],
                               "isError": True}}, 200
    return _mcp_err(_id, -32601, "Method not found: %s" % method), 200


@app.post("/mcp")
def mcp_endpoint(payload: Dict = Body(default=None), request: Request = None):
    """MCP 协议端点（Streamable HTTP · JSON-RPC 2.0）。

    支持单条与批量消息（数组）；通知类返回 202 空体。
    """
    if not _verify_token(request):
        _OP_LOG.warning("❌ /mcp: 未授权访问 (token 不匹配)")
        raise HTTPException(status_code=403,
                            detail="Unauthorized: invalid or missing token")
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
            resp, st = _mcp_handle_one(item)
            if resp is not None:
                responses.append(resp)
            status = max(status, st) if st != 202 else status
        if not responses:
            return JSONResponse(status_code=202, content={}, headers=headers)
        return JSONResponse(status_code=200, content=responses, headers=headers)
    resp, status = _mcp_handle_one(payload or {})
    if resp is None:
        return JSONResponse(status_code=202, content={}, headers=headers)
    return JSONResponse(status_code=status, content=resp, headers=headers)


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
