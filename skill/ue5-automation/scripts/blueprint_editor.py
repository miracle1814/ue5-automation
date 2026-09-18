#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 🔴 KB 语义搜索结果（已贴注释顶部）：
#   - ConnectPins / BreakLink 跨图 Pin 操作必须校验同图归属
#   - delete_node 三步：FBlueprintEditorUtils::RemoveNode → Graph->RemoveNode → Node->DestroyNode
#   - 铁律 #3: 连接/断开结果必须检查
# 🔴 红线：无硬编码绝对路径；datetime.now() 用时间戳；写入后验证

"""
blueprint_editor.py — UE 蓝图增量修改引擎（v0.7）
=================================================
面向 UE5 蓝图的「定位节点 → 修改 pin/连线 → 删除 → 验证」完整增量编辑能力。

核心操作（4 API）：
  1. update_node_param(node_id, param_name, new_value) — 修改节点参数
  2. connect_pins_by_id(node_a, pin_a, node_b, pin_b)  — 按标识连线
  3. disconnect_pin(node_id, pin_name)                  — 断开引脚连接
  4. delete_node(node_id)                               — 删除节点（含引用检查）

通信架构：
  Agent ── HTTP ──▶ ue5_bridge.py (UE 编辑器内 :8889)
                       ├── /read       (读蓝图信息)
                       ├── /command    (白名单命令：set/get_pin_default_value 等)
                       ├── /connect    (连线，已有)
                       ├── /disconnect (断线，新增)
                       ├── /delete_node(删节点，新增)
                       └── /build      (创建蓝图)

  T-20260805-UE-FULL-CLOSURE：
    - /execute 端点已废弃（404），所有写入/验证走 /command 白名单
    - 请求头一律带 X-Skill-Token（默认解析 UE5_BRIDGE_TOKEN env → %APPDATA%/ue5_bridge/token）

安全机制：
  - 每操作前 freeze 快照（预读状态；快照失败降级为告警，不阻断操作）
  - 执行后 verify 验证
  - rollback() 回滚（v2.20 实装）：connect → 反向 /disconnect；disconnect →
    由 frozen_state.existing_links 反向 /connect 重建；update_param → 恢复
    frozen_state 旧值（若记录了 old_value）。
    ⚠️ delete_node **不可自动回滚**（节点对象无克隆 API，快照不足以重建）——
    删除操作请先用 dry_run=True 确认。
  - 事务日志 JSONL（可审计）

已知 Pitfalls 已内建防护（来自 pitfalls.md）：
  P01: ConnectPins 假阳性 → 双向 LinkedTo 验证
  P02: CONNECTPINS_SILENT_FAIL → 前置参数校验 + 后置断言
  P09: WEB_THREAD_UOBJECT → 全部通过 GameThread-safe 端点

用法：
  from blueprint_editor import BlueprintEditor

  editor = BlueprintEditor()
  editor.update_node_param("/Game/BP_Enemy", "BeginPlay", "bCanEverTick", "True")
  editor.connect_pins_by_id("/Game/BP_Enemy", "BeginPlay", "then", "PrintString", "execute")
  editor.disconnect_pin("/Game/BP_Enemy", "PrintString", "InString")
  editor.delete_node("/Game/BP_Enemy", "OldEvent")

作者：dev（代码开发 Agent）
日期：2026-07-27
版本：v0.7.0
"""

import json
import time
import os
import sys
import traceback
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ── 日志 ──────────────────────────────────────────────

log = logging.getLogger("blueprint_editor")
log.setLevel(logging.INFO)
if not log.handlers:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(h)


# ── 常量 ──────────────────────────────────────────────

DEFAULT_BRIDGE_URL = "http://127.0.0.1:8889"
DEFAULT_TIMEOUT = 30
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")


def _norm_guid_arg(value) -> str:
    """GUID 归一化（去 {} / - 去空白 + 大写），非 32 位 hex → 返回 ""。

    T-20260911-UE5BRIDGE-DELNODE-GUID：`node_id` 兼容「节点标题 / 32 位 hex GUID」
    两种形态。仅当入参确实形如 GUID 时才返回非空串，避免把普通标题误判成 GUID。
    """
    s = str(value or "")
    for ch in ("{", "}", "-", " ", "\t"):
        s = s.replace(ch, "")
    s = s.strip().upper()
    if len(s) != 32:
        return ""
    return s if all(c in "0123456789ABCDEF" for c in s) else ""


# ── 数据模型 ──────────────────────────────────────────

@dataclass
class EditOperation:
    """单次编辑操作记录。"""
    op_type: str           # update_param / connect / disconnect / delete
    blueprint_path: str
    params: Dict[str, Any]
    frozen_state: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    status: str = "PENDING"  # PENDING / SUCCESS / FAILED / ROLLED_BACK
    error: str = ""
    timestamp: str = ""
    elapsed_ms: float = 0.0

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class EditResult:
    """编辑操作统一返回。"""
    success: bool
    operation: str
    details: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    elapsed_ms: float = 0.0


@dataclass
class ConnectionRef:
    """连线引用。"""
    from_node: str
    from_pin: str
    to_node: str
    to_pin: str


# ── BlueprintEditor ───────────────────────────────────

def _resolve_default_token() -> Optional[str]:
    """解析默认桥接 token。

    T-20260805-UE-FULL-CLOSURE · P0-1：
      优先读 UE5_BRIDGE_TOKEN 环境变量 → 兜底 %APPDATA%/ue5_bridge/token 文件
      （与 ue5_bridge.py 的 token 生成/存储路径一致，见 ue5_bridge.py L115-128）
    """
    tok = os.environ.get("UE5_BRIDGE_TOKEN")
    if tok:
        return tok.strip() or None
    tok_file = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "ue5_bridge", "token",
    )
    try:
        with open(tok_file, "r", encoding="utf-8") as f:
            t = f.read().strip()
            return t or None
    except OSError:
        return None


class BlueprintEditor:
    """蓝图增量修改编辑器。

    封装 HTTP 桥通信 + 操作安全防护 + 事务记录。

    参数:
        bridge_url: ue5_bridge.py 地址（默认 http://127.0.0.1:8889）
        auto_snapshot: 是否每次操作前自动快照（默认 True）
        log_dir: 事务日志目录
        token: X-Skill-Token 认证令牌；默认 None → 自动解析
               （UE5_BRIDGE_TOKEN env → %APPDATA%/ue5_bridge/token 兜底）
    """

    def __init__(self,
                 bridge_url: str = DEFAULT_BRIDGE_URL,
                 auto_snapshot: bool = True,
                 log_dir: str = None,
                 token: Optional[str] = None):
        self.bridge_url = bridge_url.rstrip("/")
        self.auto_snapshot = auto_snapshot
        self.log_dir = log_dir or LOG_DIR
        self.token = token if token is not None else _resolve_default_token()
        # v2.20 修复（P2）：skill 安装在只读目录（如 Program Files）时
        #   makedirs 直接 PermissionError → 构造失败 → 4 个 API 全不可用。
        #   回退到 %APPDATA%/ue5_bridge/logs（token 已在此目录），再失败则
        #   禁用文件事务日志（仅内存 history），构造不失败。
        try:
            os.makedirs(self.log_dir, exist_ok=True)
        except OSError as e:
            _fb = os.path.join(
                os.environ.get("APPDATA", os.path.expanduser("~")),
                "ue5_bridge", "logs")
            try:
                os.makedirs(_fb, exist_ok=True)
                log.warning("事务日志目录不可写（%s: %s）→ 回退 %s", self.log_dir, e, _fb)
                self.log_dir = _fb
            except OSError as e2:
                log.warning("事务日志目录不可写（%s / %s）→ 禁用文件日志（仅内存 history）",
                            self.log_dir, e2)
                self.log_dir = None

        # 操作历史
        self._history: List[EditOperation] = []
        self._snapshots: Dict[str, Dict[str, Any]] = {}  # bp_path → last snapshot

    # ── HTTP 通信 ─────────────────────────────────────

    def _auth_headers(self) -> Dict[str, str]:
        """构造带 Token 认证的请求头（T-20260805-UE-FULL-CLOSURE P0-1）。"""
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Skill-Token"] = self.token
        return headers

    def _post(self, endpoint: str, data: Dict[str, Any],
              timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
        """POST 请求到 ue5_bridge。"""
        url = f"{self.bridge_url}{endpoint}"
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers=self._auth_headers(),
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} from {url}: {err_body}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"无法连接 {url}: {e.reason}")

    def _get(self, endpoint: str, timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
        """GET 请求到 ue5_bridge。"""
        url = f"{self.bridge_url}{endpoint}"
        req = urllib.request.Request(
            url, headers=self._auth_headers(), method="GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"无法连接 {url}: {e.reason}")

    # ── 快照管理 ──────────────────────────────────────

    def snapshot(self, blueprint_path: str) -> Dict[str, Any]:
        """读取蓝图当前状态快照。

        Returns:
            蓝图 L1 信息 + 节点清单 + 连线拓扑的 dict
        """
        bp_path = self._normalize_path(blueprint_path)
        result = self._post("/read", {"blueprint_path": bp_path, "level": "L1"})
        if not result.get("success"):
            raise RuntimeError(f"快照失败: {result}")
        data = result.get("data", {})

        # 补充 L2 节点清单（通过 /command read_nodes 白名单）
        try:
            l2 = self._get_node_list(bp_path)
            data["_nodes"] = l2
        except Exception as e:
            log.warning(f"L2 快照失败（non-fatal）: {e}")
            data["_nodes"] = []

        self._snapshots[bp_path] = data
        return data

    def _get_node_list(self, blueprint_path: str) -> List[Dict[str, Any]]:
        """通过 /command read_nodes 白名单获取节点清单。

        T-20260805-UE-FULL-CLOSURE：替代旧 /execute 路径（404 端点已废弃），
        供 snapshot() 补充 L2 节点清单。
        """
        try:
            raw = self._post("/command", {
                "command": "read_nodes",
                "params": {"bp_path": blueprint_path},
                "timeout": 15.0,
            })
            if not raw.get("success"):
                return []
            result = raw.get("result") or {}
            if not result.get("available"):
                return []
            nodes = []
            for g in result.get("graphs", []):
                for n in g.get("nodes", []):
                    nodes.append({
                        "graph": g.get("graph_name"),
                        "title": n.get("title"),
                        "class": n.get("class"),
                        "guid": n.get("guid"),
                    })
            return nodes
        except Exception:
            return []

    def _normalize_path(self, path: str) -> str:
        """标准化蓝图路径。"""
        path = path.strip()
        if path.endswith("_C"):
            path = path[:-2]
        return path

    # ── 操作记录 ──────────────────────────────────────

    def _record(self, op: EditOperation):
        """记录操作到历史 + JSONL 日志（log_dir 不可用时仅内存 history）。"""
        self._history.append(op)
        if not self.log_dir:
            return
        try:
            log_file = os.path.join(self.log_dir, "blueprint_editor.jsonl")
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(op.to_jsonl() + "\n")
        except OSError as e:
            log.warning("事务日志写入失败（仅保留内存 history）: %s", e)

    # ═══════════════════════════════════════════════════
    # 核心 API 1: update_node_param
    # ═══════════════════════════════════════════════════

    def update_node_param(self,
                          blueprint_path: str,
                          node_id: str,
                          param_name: str,
                          new_value: str) -> EditResult:
        """修改蓝图节点的引脚默认值。

        通过 C++ Bridge SetPinDefaultValue 实现，线程安全。

        Args:
            blueprint_path: 蓝图资产路径，如 /Game/BP_Enemy
            node_id: 节点标题（FindNodeByTitle 精确匹配），如 "PrintString"
            param_name: 引脚名，如 "InString"
            new_value: 新默认值，如 "Hello World"

        Returns:
            EditResult
        """
        t0 = time.time()
        bp_path = self._normalize_path(blueprint_path)

        op = EditOperation(
            op_type="update_param",
            blueprint_path=bp_path,
            params={"node_id": node_id, "param_name": param_name, "new_value": new_value},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        try:
            # Step 1: Freeze — 读当前值
            # v2.20：额外记录 pin 旧值（old_value）→ rollback() 可恢复 update_param
            if self.auto_snapshot:
                try:
                    op.frozen_state = self.snapshot(bp_path)
                    _old = self._get_pin_default_value(bp_path, node_id, param_name)
                    if _old is not None:
                        op.frozen_state = dict(op.frozen_state or {})
                        op.frozen_state["old_value"] = _old
                except Exception as e:
                    log.warning(f"快照跳过（non-fatal）: {e}")

            # Step 2: Execute — 走 /command 白名单 set_pin_default_value
            # T-20260805-UE-FULL-CLOSURE：彻底废弃 /execute（404 端点），
            # 白名单命令内部用真实 DLL 3 参签名 SetPinDefaultValue(Node, Pin, Value)
            raw = self._post("/command", {
                "command": "set_pin_default_value",
                "params": {
                    "bp_path": bp_path,
                    "node_id": node_id,
                    "pin_name": param_name,
                    "new_value": new_value,
                },
                "timeout": 30.0,
            })
            if not raw.get("success"):
                raise RuntimeError(f"set_pin_default_value 失败: {raw.get('error', 'unknown')}")

            # Step 3: Verify — 回读确认
            verified = self._verify_param(bp_path, node_id, param_name, new_value)

            elapsed = (time.time() - t0) * 1000
            op.status = "SUCCESS"
            op.result = {"verified": verified}
            op.elapsed_ms = elapsed

            self._record(op)
            return EditResult(
                success=True,
                operation="update_param",
                details=f"✅ {node_id}.{param_name} = {new_value}" + (
                    " (已验证)" if verified else " (⚠️ 未验证)"),
                data={"node": node_id, "pin": param_name, "value": new_value, "verified": verified},
                elapsed_ms=elapsed,
            )

        except Exception as e:
            elapsed = (time.time() - t0) * 1000
            op.status = "FAILED"
            op.error = str(e)
            op.elapsed_ms = elapsed
            self._record(op)

            return EditResult(
                success=False,
                operation="update_param",
                details=f"❌ {node_id}.{param_name} 修改失败: {e}",
                errors=[str(e)],
                elapsed_ms=elapsed,
            )

    def _get_pin_default_value(self, bp_path: str, node_id: str,
                               param_name: str) -> Optional[str]:
        """读取引脚当前默认值（/command get_pin_default_value）；失败返回 None。"""
        try:
            raw = self._post("/command", {
                "command": "get_pin_default_value",
                "params": {
                    "bp_path": bp_path,
                    "node_id": node_id,
                    "pin_name": param_name,
                },
                "timeout": 15.0,
            })
            if raw.get("success"):
                result = raw.get("result") or {}
                if result.get("ok"):
                    return str(result.get("value"))
            return None
        except Exception:
            return None

    def _verify_param(self, bp_path: str, node_id: str,
                      param_name: str, expected_value: str) -> bool:
        """验证参数修改是否生效（走 /command get_pin_default_value 白名单）。"""
        try:
            raw = self._post("/command", {
                "command": "get_pin_default_value",
                "params": {
                    "bp_path": bp_path,
                    "node_id": node_id,
                    "pin_name": param_name,
                },
                "timeout": 15.0,
            })
            if raw.get("success"):
                result = raw.get("result") or {}
                return str(result.get("value")) == str(expected_value)
            return False
        except Exception:
            return False

    # ═══════════════════════════════════════════════════
    # 核心 API 2: connect_pins_by_id
    # ═══════════════════════════════════════════════════

    def connect_pins_by_id(self,
                           blueprint_path: str,
                           node_a: str,
                           pin_a: str,
                           node_b: str,
                           pin_b: str) -> EditResult:
        """按节点标题 + 引脚名连接两个引脚。

        通过已有 /connect 端点实现，含双向 LinkedTo 验证。

        Args:
            blueprint_path: 蓝图资产路径
            node_a: 源节点标题
            pin_a: 源引脚名
            node_b: 目标节点标题
            pin_b: 目标引脚名

        Returns:
            EditResult
        """
        t0 = time.time()
        bp_path = self._normalize_path(blueprint_path)

        op = EditOperation(
            op_type="connect",
            blueprint_path=bp_path,
            params={"src_node": node_a, "src_pin": pin_a, "dst_node": node_b, "dst_pin": pin_b},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        try:
            # Step 1: Freeze
            if self.auto_snapshot:
                try:
                    op.frozen_state = self.snapshot(bp_path)
                except Exception as e:
                    log.warning(f"快照跳过: {e}")

            # Step 2: Execute — 通过 /connect
            result = self._post("/connect", {
                "blueprint_path": bp_path,
                "connections": [{
                    "src_node": node_a, "src_pin": pin_a,
                    "dst_node": node_b, "dst_pin": pin_b,
                }]
            })

            # v2.20：`results` 键存在但为空列表时 `result.get("results", [{}])[0]`
            #   会 IndexError（被外层吞成「连线失败」错误信息失真）→ 安全取首项
            _results = result.get("results")
            conn_result = (_results[0] if _results else {}) if isinstance(_results, list) else {}
            # T-20260805 Bug2: 兼容两种返回结构——真实 /connect 端点 results 在顶层
            #   （{"success": True, "results": [...]}），部分 mock/历史实现包在
            #   {"success": True, "data": {"results": [...]}} 里
            if not conn_result or "status" not in conn_result:
                _d = result.get("data")
                if isinstance(_d, dict) and _d.get("results"):
                    conn_result = (_d["results"] or [{}])[0]
            status = conn_result.get("status", "unknown")

            elapsed = (time.time() - t0) * 1000
            op.result = conn_result

            if status in ("connected", "unverified"):
                op.status = "SUCCESS"
                op.elapsed_ms = elapsed
                self._record(op)
                return EditResult(
                    success=True,
                    operation="connect",
                    details=f"✅ {node_a}.{pin_a} → {node_b}.{pin_b} ({status})",
                    data=conn_result,
                    warnings=[] if status == "connected" else ["未完全验证"],
                    elapsed_ms=elapsed,
                )
            else:
                op.status = "FAILED"
                op.error = f"连线状态: {status} (reason: {conn_result.get('reason', conn_result.get('error', 'unknown'))})"
                op.elapsed_ms = elapsed
                self._record(op)
                return EditResult(
                    success=False,
                    operation="connect",
                    details=f"❌ {node_a}.{pin_a} → {node_b}.{pin_b}: {status}",
                    errors=[op.error],
                    elapsed_ms=elapsed,
                )

        except Exception as e:
            elapsed = (time.time() - t0) * 1000
            op.status = "FAILED"
            op.error = str(e)
            op.elapsed_ms = elapsed
            self._record(op)
            return EditResult(
                success=False,
                operation="connect",
                details=f"❌ 连线失败: {e}",
                errors=[str(e)],
                elapsed_ms=elapsed,
            )

    # ═══════════════════════════════════════════════════
    # 核心 API 3: disconnect_pin
    # ═══════════════════════════════════════════════════

    def disconnect_pin(self,
                       blueprint_path: str,
                       node_id: str,
                       pin_name: str) -> EditResult:
        """断开指定节点的指定引脚的全部连线。

        通过 /disconnect 端点实现（GameThread 安全）。
        断开前记录所有现有连线，以便回滚。

        Args:
            blueprint_path: 蓝图资产路径
            node_id: 节点标题
            pin_name: 引脚名

        Returns:
            EditResult
        """
        t0 = time.time()
        bp_path = self._normalize_path(blueprint_path)

        op = EditOperation(
            op_type="disconnect",
            blueprint_path=bp_path,
            params={"node_id": node_id, "pin_name": pin_name},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        try:
            # Step 1: Freeze — 记录当前连线
            existing_links = self._get_pin_links(bp_path, node_id, pin_name)
            op.frozen_state = {"existing_links": existing_links or []}

            if existing_links is None:
                # v2.20 修复（P1 假成功）：连线读取失败 ≠ 本就无连接。
                #   旧实现把读取失败也当空列表 → 直接谎报 success=True 且不调
                #   /disconnect。现在读取失败时记录告警并直调 /disconnect，
                #   由服务端权威定论（节点/pin 真不存在会得到结构化失败）。
                _read_warn = ("连线快照读取失败（read_connections 不可用）→ "
                              "跳过本地预判，直调 /disconnect 定论")
                log.warning(_read_warn)

            if existing_links == []:
                elapsed = (time.time() - t0) * 1000
                op.status = "SUCCESS"
                op.result = {"broken_links": [], "already_disconnected": True}
                op.elapsed_ms = elapsed
                self._record(op)
                return EditResult(
                    success=True,
                    operation="disconnect",
                    details=f"✅ {node_id}.{pin_name} 本就无连接",
                    data={"broken_links": [], "already_disconnected": True},
                    elapsed_ms=elapsed,
                )
            # existing_links is None（读取失败）→ 落到下方 /disconnect 权威路径

            # Step 2: Execute — 通过 /disconnect
            result = self._post("/disconnect", {
                "blueprint_path": bp_path,
                "node_id": node_id,
                "pin_name": pin_name,
            })

            elapsed = (time.time() - t0) * 1000
            data = result.get("data", {})

            if result.get("success"):
                # 🔴 T-20260727-UE05-V07-FIX 问题 1：
                # /disconnect 端点不管 actually_broken 是否为空都返回 success=True
                # 当 break_link_to 失败时，方法降级到 catalog_only（仅记录未真断）
                # 必须将 catalog_only 标记为 PARTIAL → success=False + warnings
                method = data.get("method", "python_api")
                if method == "catalog_only":
                    op.status = "PARTIAL"
                    op.error = "Bridge 降级到 catalog_only：仅记录未真断"
                    op.result = data
                    op.elapsed_ms = elapsed
                    self._record(op)
                    return EditResult(
                        success=False,
                        operation="disconnect",
                        details=f"⚠️ {node_id}.{pin_name} 降级到 catalog_only（未真断）",
                        data=data,
                        warnings=[op.error],
                        elapsed_ms=elapsed,
                    )

                op.status = "SUCCESS"
                op.result = data
                op.elapsed_ms = elapsed
                self._record(op)
                broken = data.get("broken_links", [])
                return EditResult(
                    success=True,
                    operation="disconnect",
                    details=f"✅ {node_id}.{pin_name} 断开 {len(broken)} 条连线",
                    data=data,
                    elapsed_ms=elapsed,
                )
            else:
                op.status = "FAILED"
                op.error = result.get("detail", "disconnect failed")
                op.elapsed_ms = elapsed
                self._record(op)
                return EditResult(
                    success=False,
                    operation="disconnect",
                    details=f"❌ {node_id}.{pin_name} 断开失败",
                    errors=[op.error],
                    elapsed_ms=elapsed,
                )

        except Exception as e:
            elapsed = (time.time() - t0) * 1000
            op.status = "FAILED"
            op.error = str(e)
            op.elapsed_ms = elapsed
            self._record(op)
            return EditResult(
                success=False,
                operation="disconnect",
                details=f"❌ 断开失败: {e}",
                errors=[str(e)],
                elapsed_ms=elapsed,
            )

    def _get_pin_links(self, bp_path: str, node_id: str,
                       pin_name: str) -> Optional[List[Dict[str, str]]]:
        """获取引脚当前所有连线（走 /command read_connections 白名单）。

        T-20260805-UE-FULL-CLOSURE：替代旧 /execute 路径（404 端点已废弃）。

        v2.20 修复（P1 假成功）：
          · `node_id` 为 32 位 GUID 时按 from_guid/to_guid 精确匹配（bridge 侧
            read_connections 已回传 GUID；旧实现只按标题匹配 → GUID 入参永远
            落空 → 假「本就无连接」且不调 /disconnect）。
          · 读取失败（HTTP 错误 / success=False / available=False / 异常）返回
            **None**（区别于「确实无连线」的空列表）→ disconnect_pin 收到 None
            时不再谎报「本就无连接」，改为直调 /disconnect 由服务端定论。
        """
        target_guid = _norm_guid_arg(node_id)
        try:
            raw = self._post("/command", {
                "command": "read_connections",
                "params": {"bp_path": bp_path, "graph_name": "EventGraph"},
                "timeout": 15.0,
            })
            if not raw.get("success"):
                return None
            result = raw.get("result") or {}
            if not result.get("available"):
                return None
            links = []
            for g in result.get("graphs", []):
                for c in g.get("connections", []):
                    if target_guid:
                        _hit = (_norm_guid_arg(c.get("from_guid")) == target_guid
                                and c.get("from_pin") == pin_name) or \
                               (_norm_guid_arg(c.get("to_guid")) == target_guid
                                and c.get("to_pin") == pin_name)
                    else:
                        _hit = (c.get("from_node") == node_id and c.get("from_pin") == pin_name) or \
                               (c.get("to_node") == node_id and c.get("to_pin") == pin_name)
                    if _hit:
                        links.append({
                            "from": c.get("from_node"), "from_pin": c.get("from_pin"),
                            "to": c.get("to_node"), "to_pin": c.get("to_pin"),
                        })
            return links
        except Exception:
            return None

    # ═══════════════════════════════════════════════════
    # 核心 API 4: delete_node
    # ═══════════════════════════════════════════════════

    def delete_node(self,
                    blueprint_path: str,
                    node_id: str,
                    dry_run: bool = False) -> EditResult:
        """删除蓝图中的指定节点。

        通过 /delete_node 端点实现，自动执行：
          1. 枚举该节点所有连线并断开
          2. 从图中移除节点
          3. 销毁节点对象
          4. 标记蓝图脏

        Args:
            blueprint_path: 蓝图资产路径
            node_id: 节点标题，**或 32 位 hex GUID**（T-20260911-UE5BRIDGE-DELNODE-GUID）
                ⚠️ 图中存在同名节点（标题命中 > 1）时**必须传 GUID**：传标题会被
                `/delete_node` 端点 fail-loud 拒绝（回传全部同名节点 GUID 列表），
                绝不猜测目标。
            dry_run: 仅检查引用关系，不实际删除

        Returns:
            EditResult
        """
        t0 = time.time()
        bp_path = self._normalize_path(blueprint_path)

        op = EditOperation(
            op_type="delete",
            blueprint_path=bp_path,
            params={"node_id": node_id, "dry_run": dry_run},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        try:
            # Step 1: Freeze — 记录节点当前状态
            existing_info = self._get_node_info(bp_path, node_id)
            op.frozen_state = {"node_info": existing_info}

            if not existing_info:
                elapsed = (time.time() - t0) * 1000
                op.status = "FAILED"
                op.error = f"节点不存在: {node_id}"
                op.elapsed_ms = elapsed
                self._record(op)
                return EditResult(
                    success=False,
                    operation="delete",
                    details=f"❌ 节点不存在: {node_id}",
                    errors=[op.error],
                    elapsed_ms=elapsed,
                )

            if dry_run:
                # 干跑：仅统计受影响连线
                affected = existing_info.get("pin_count", 0)
                connected = existing_info.get("connected_pin_count", 0)
                elapsed = (time.time() - t0) * 1000
                op.status = "SUCCESS"
                op.result = {"dry_run": True, **existing_info}
                op.elapsed_ms = elapsed
                self._record(op)
                return EditResult(
                    success=True,
                    operation="delete",
                    details=f"🔍 干跑: {node_id} — {affected} 引脚 ({connected} 已连线)，可安全删除",
                    data={"dry_run": True, **existing_info},
                    elapsed_ms=elapsed,
                )

            # Step 2: Execute — 通过 /delete_node
            result = self._post("/delete_node", {
                "blueprint_path": bp_path,
                "node_id": node_id,
            })

            elapsed = (time.time() - t0) * 1000
            data = result.get("data", {})

            if result.get("success"):
                op.status = "SUCCESS"
                op.result = data
                op.elapsed_ms = elapsed
                self._record(op)
                return EditResult(
                    success=True,
                    operation="delete",
                    details=f"✅ 已删除 {node_id}（断开 {data.get('broken_links', 0)} 条连线）",
                    data=data,
                    elapsed_ms=elapsed,
                )
            else:
                op.status = "FAILED"
                op.error = result.get("detail", "delete_node failed")
                op.elapsed_ms = elapsed
                self._record(op)
                return EditResult(
                    success=False,
                    operation="delete",
                    details=f"❌ {node_id} 删除失败",
                    errors=[op.error],
                    elapsed_ms=elapsed,
                )

        except Exception as e:
            elapsed = (time.time() - t0) * 1000
            op.status = "FAILED"
            op.error = str(e)
            op.elapsed_ms = elapsed
            self._record(op)
            return EditResult(
                success=False,
                operation="delete",
                details=f"❌ 删除失败: {e}",
                errors=[str(e)],
                elapsed_ms=elapsed,
            )

    def _get_node_info(self, bp_path: str, node_id: str) -> Optional[Dict[str, Any]]:
        """获取节点基本信息（用于冻结/干跑）。

        T-20260805-UE-FULL-CLOSURE：走 /command read_nodes 白名单，
        替代旧 /execute 路径（404 端点已废弃）。

        v2.20 修复（P1）：
          · 不再限定 graph_name="EventGraph"（bridge 语义：graph_name 有值 →
            只返回该图）→ 函数图 / 宏 / 构造脚本中的节点此前永远报「节点不存在」；
            不传 → 全图搜索。
          · 读取失败（success=False / available=False / 异常）抛 RuntimeError，
            与「节点真的不存在」（返回 {"node_exists": False}）可区分 ——
            旧实现一律吞掉 → delete_node 误报「节点不存在」，排障困难。
        """
        try:
            raw = self._post("/command", {
                "command": "read_nodes",
                "params": {"bp_path": bp_path},
                "timeout": 15.0,
            })
            if not raw.get("success"):
                raise RuntimeError("read_nodes 失败: %s" % (raw.get("detail") or raw.get("error") or raw))
            result = raw.get("result") or {}
            if not result.get("available"):
                raise RuntimeError("read_nodes 不可用（bridge L2 读取通道异常）")
            target_guid = _norm_guid_arg(node_id)
            for g in result.get("graphs", []):
                for n in g.get("nodes", []):
                    # T-20260911-UE5BRIDGE-DELNODE-GUID：支持 GUID 形态入参
                    #   （标题精确匹配 **或** GUID 归一化匹配，二者取或）
                    if (n.get("title") == node_id
                            or (target_guid
                                and _norm_guid_arg(n.get("guid")) == target_guid)):
                        pins = n.get("pins", [])
                        connected = sum(1 for p in pins if p.get("is_connected"))
                        return {
                            "title": n.get("title"),
                            "class": n.get("class"),
                            "guid": n.get("guid"),
                            "graph_name": g.get("graph_name"),
                            "pin_count": len(pins),
                            "connected_pin_count": connected,
                            "node_exists": True,
                        }
            return {"node_exists": False}
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError("read_nodes 调用异常: %s" % e)

    # ── 便捷方法 ──────────────────────────────────────

    def health_check(self) -> Dict[str, Any]:
        """Bridge 健康检查。"""
        return self._get("/health")

    def get_history(self) -> List[EditOperation]:
        """获取操作历史。"""
        return list(self._history)

    def rollback(self, last_n: int = 1) -> Dict[str, Any]:
        """回滚最近 N 次**可逆**操作（v2.20 实装）。

        回滚语义（按操作类型）：
          · connect      → 反向 /disconnect（断开刚建立的连线）
          · disconnect   → 由 frozen_state.existing_links 逐条 /connect 重建
          · update_param → frozen_state 记录了 old_value 时恢复之
          · delete       → ⛔ 不可回滚（节点无克隆 API）——跳过并在 skipped 中报告

        Returns:
            {"rolled_back": [op...], "skipped": [{"op":..., "reason":...}],
             "errors": [{"op":..., "error":...}]}
        """
        rolled, skipped, errors = [], [], []
        candidates = [op for op in self._history
                      if op.status in ("SUCCESS", "PARTIAL")][-max(1, int(last_n)):]
        # 逆序回滚（后发生的先回滚）
        for op in reversed(candidates):
            try:
                if op.op_type == "connect":
                    p = op.params
                    res = self._post("/disconnect", {
                        "blueprint_path": op.blueprint_path,
                        "node_id": p.get("dst_node", ""),
                        "pin_name": p.get("dst_pin", ""),
                    })
                    if res.get("success") or res.get("data", {}).get("already_disconnected"):
                        op.status = "ROLLED_BACK"
                        rolled.append(op)
                    else:
                        errors.append({"op": op.op_type, "error": res.get("detail", "disconnect failed")})
                elif op.op_type == "disconnect":
                    links = (op.frozen_state or {}).get("existing_links") or []
                    if not links:
                        op.status = "ROLLED_BACK"   # 原本就无连线 → 无需重建
                        rolled.append(op)
                        continue
                    conns = [{"src_node": l["from"], "src_pin": l["from_pin"],
                              "dst_node": l["to"], "dst_pin": l["to_pin"]} for l in links]
                    res = self._post("/connect", {
                        "blueprint_path": op.blueprint_path, "connections": conns})
                    _ok = bool(res.get("success")) and all(
                        c.get("status") == "connected"
                        for c in (res.get("results") or [{"status": "unknown"}]))
                    if _ok:
                        op.status = "ROLLED_BACK"
                        rolled.append(op)
                    else:
                        errors.append({"op": op.op_type, "error": "重连未全部验证通过: %s" % res.get("detail", "")})
                elif op.op_type == "update_param":
                    old = (op.frozen_state or {}).get("old_value")
                    if old is None:
                        skipped.append({"op": op.op_type, "reason": "frozen_state 未记录旧值，无法恢复"})
                        continue
                    p = op.params
                    res = self._post("/command", {
                        "command": "set_pin_default_value",
                        "params": {"bp_path": op.blueprint_path,
                                   "node_id": p.get("node_id", ""),
                                   "pin_name": p.get("param_name", ""),
                                   "new_value": str(old)}})
                    if res.get("success") and (res.get("result") or {}).get("ok"):
                        op.status = "ROLLED_BACK"
                        rolled.append(op)
                    else:
                        errors.append({"op": op.op_type, "error": str(res.get("detail") or res.get("result"))})
                else:  # delete 等不可逆操作
                    skipped.append({"op": op.op_type, "reason": "该操作类型不可自动回滚（delete 无克隆 API）"})
            except Exception as e:
                errors.append({"op": op.op_type, "error": str(e)})
        return {"rolled_back": rolled, "skipped": skipped, "errors": errors}

    def clear_history(self):
        """清除操作历史（不含 JSONL 日志）。"""
        self._history.clear()
        self._snapshots.clear()


# ── 模块级便捷函数 ────────────────────────────────────

def update_node_param(blueprint_path: str, node_id: str,
                      param_name: str, new_value: str,
                      bridge_url: str = DEFAULT_BRIDGE_URL) -> EditResult:
    """快速修改节点参数（无需创建 BlueprintEditor 实例）。"""
    editor = BlueprintEditor(bridge_url=bridge_url)
    return editor.update_node_param(blueprint_path, node_id, param_name, new_value)


def connect_pins_by_id(blueprint_path: str,
                       node_a: str, pin_a: str,
                       node_b: str, pin_b: str,
                       bridge_url: str = DEFAULT_BRIDGE_URL) -> EditResult:
    """快速连线。"""
    editor = BlueprintEditor(bridge_url=bridge_url)
    return editor.connect_pins_by_id(blueprint_path, node_a, pin_a, node_b, pin_b)


def disconnect_pin(blueprint_path: str, node_id: str,
                   pin_name: str,
                   bridge_url: str = DEFAULT_BRIDGE_URL) -> EditResult:
    """快速断线。"""
    editor = BlueprintEditor(bridge_url=bridge_url)
    return editor.disconnect_pin(blueprint_path, node_id, pin_name)


def delete_node(blueprint_path: str, node_id: str,
                dry_run: bool = False,
                bridge_url: str = DEFAULT_BRIDGE_URL) -> EditResult:
    """快速删除节点。

    `node_id` = 节点标题或 32 位 hex GUID；图中同名节点（标题命中 > 1）必须传 GUID，
    否则 /delete_node 端点 fail-loud 拒绝执行。
    """
    editor = BlueprintEditor(bridge_url=bridge_url)
    return editor.delete_node(blueprint_path, node_id, dry_run=dry_run)


# ── CLI 入口 ──────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="UE 蓝图增量修改工具 (v0.7)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python blueprint_editor.py update /Game/BP_Test PrintString InString "Hello"
  python blueprint_editor.py connect /Game/BP_Test BeginPlay then PrintString execute
  python blueprint_editor.py disconnect /Game/BP_Test PrintString InString
  python blueprint_editor.py delete /Game/BP_Test OldNode --dry-run
  python blueprint_editor.py delete /Game/BP_Test OldNode
  python blueprint_editor.py delete /Game/BP_Test 3A0454FC4E2D85BE3F3C78B1523470C3  # 同名节点 → 传 GUID
        """
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # update
    p_update = sub.add_parser("update", help="修改节点参数")
    p_update.add_argument("blueprint_path")
    p_update.add_argument("node_id")
    p_update.add_argument("param_name")
    p_update.add_argument("new_value")

    # connect
    p_connect = sub.add_parser("connect", help="连线")
    p_connect.add_argument("blueprint_path")
    p_connect.add_argument("node_a")
    p_connect.add_argument("pin_a")
    p_connect.add_argument("node_b")
    p_connect.add_argument("pin_b")

    # disconnect
    p_disconnect = sub.add_parser("disconnect", help="断线")
    p_disconnect.add_argument("blueprint_path")
    p_disconnect.add_argument("node_id")
    p_disconnect.add_argument("pin_name")

    # delete
    p_delete = sub.add_parser("delete", help="删除节点")
    p_delete.add_argument("blueprint_path")
    p_delete.add_argument(
        "node_id",
        help="节点标题，或 32 位 hex GUID（图中存在同名节点时必须传 GUID；"
             "传标题命中多个同名节点会被 fail-loud 拒绝）")
    p_delete.add_argument("--dry-run", action="store_true")

    # health
    p_health = sub.add_parser("health", help="健康检查")

    args = parser.parse_args()
    # v2.20 修复（P2）：中文 Windows 控制台默认 GBK，输出含 ✅/❌/⚠️ 等 emoji 时
    #   UnicodeEncodeError 崩溃（blueprint_reader.py 已有同款修复，此处对齐）。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    editor = BlueprintEditor()

    if args.command == "health":
        result = editor.health_check()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    cmd_map = {
        "update": lambda: editor.update_node_param(
            args.blueprint_path, args.node_id, args.param_name, args.new_value),
        "connect": lambda: editor.connect_pins_by_id(
            args.blueprint_path, args.node_a, args.pin_a, args.node_b, args.pin_b),
        "disconnect": lambda: editor.disconnect_pin(
            args.blueprint_path, args.node_id, args.pin_name),
        "delete": lambda: editor.delete_node(
            args.blueprint_path, args.node_id, dry_run=args.dry_run),
    }

    fn = cmd_map.get(args.command)
    if fn:
        result = fn()
        print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
        sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
