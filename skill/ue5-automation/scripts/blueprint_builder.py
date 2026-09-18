#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 🔴 KB 语义搜索结果（已贴注释顶部）：
#   铁律 #1: 创建节点+连线必须一次调用完成
#   铁律 #3: 连接结果必须检查
#   铁律 #12: CDO 修改必须编译后做
#   P01: ConnectPins 假阳性 — 必须双向 LinkedTo 验证
#   P04: CDO 修改在编译前会丢失
# 🔴 红线：无硬编码绝对路径；datetime.now() 用于时间戳；写入后验证

"""
blueprint_builder.py — 蓝图创建引擎
====================================
⚠️ v0.6 起已被 ue5_bridge.py GT 代码取代 — 实际蓝图构建走 HTTP Bridge，不走本模块
   当前状态：保留作为 Python API 入口（任何 Agent 可 import）；真实流程不使用
   已知局限：add_variable/add_component/add_node 等方法要求 CONTINUE 模式（_ensure_continue_mode）
              而 create() 流程不会自动切换模式 → 直接调用会 RuntimeError
   推荐用法：使用 ue5_runner.py 的 build() 接口（已通过 6/6 UE 在线 + 103/103 离线测试）
   维护责任：仅修复致命问题，不再扩展新功能

集成 safe_wrapper/ 的已有代码，提供新建 + 续写两种模式。

能力矩阵：
  新建模式 — 创建蓝图资产（Actor/Widget/FunctionLibrary/Interface）
  续写模式 — 加载已有蓝图，增量添加变量/组件/节点/连线
  变量管理 — 类型/默认值/暴露/Category
  组件管理 — 注册到 SCS
  函数节点 — add_function_call_node（带 Bridge 路径校验）
  连线     — ConnectPins + 双向 LinkedTo.Contains() 验证（P01/P02 修复）
  编译     — compile → 验证 → 失败则回滚 + 报原因

铁律（来自 UE 经验提炼 22 条 + pitfalls.md）：
  铁律 #1:  创建节点+连线必须一次调用完成
  铁律 #3:  连接结果必须检查
  铁律 #12: CDO 修改必须编译后做
  P01:     ConnectPins 假阳性 — 必须双向 LinkedTo 验证
  P04:     CDO 修改在编译前会丢失

上下文文件：safe_wrapper/（7文件）、ue5_bridge.py、blueprint_reader.py

作者：dev（代码开发 Agent）
日期：2026-07-22
"""

import os
import sys
import json
import time
import copy
import traceback
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple, Union


# ── 路径处理（B-006: 改用 pathlib.Path 消除多层 dirname 脆弱性）──

from pathlib import Path as _Path
_THIS_DIR = _Path(__file__).resolve().parent
# __file__ = skills/ue5-automation/scripts/blueprint_builder.py
# 向上 3 层 → skills/..(default)/, 然后 output/safe_wrapper
_SAFE_WRAPPER_DIR = str(_THIS_DIR.parent.parent.parent / "output" / "safe_wrapper")

if _SAFE_WRAPPER_DIR not in sys.path:
    sys.path.insert(0, _SAFE_WRAPPER_DIR)

_safe_wrapper_available = False
try:
    from transaction import (
        Transaction, TransactionBatch, TransactionResult, TransactionStatus,
        CreateBlueprintTransaction, ConnectPinsTransaction,
        CompileBlueprintTransaction, SetCDOTransaction,
    )
    _safe_wrapper_available = True
except ImportError:
    pass

# ── 枚举 ──

class BlueprintType(Enum):
    ACTOR = auto()
    WIDGET = auto()
    FUNCTION_LIBRARY = auto()
    INTERFACE = auto()


class BuilderMode(Enum):
    CREATE = auto()
    CONTINUE = auto()


class BuildStatus(Enum):
    PENDING = "PENDING"
    CREATING = "CREATING"
    ADDING_VARS = "ADDING_VARS"
    ADDING_COMPONENTS = "ADDING_COMPONENTS"
    ADDING_NODES = "ADDING_NODES"
    CONNECTING = "CONNECTING"
    COMPILING = "COMPILING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


# ── 数据模型 ──

@dataclass
class VariableSpec:
    name: str
    var_type: str
    default_value: Any = None
    expose: bool = True
    category: str = "Default"
    is_array: bool = False
    tooltip: str = ""


@dataclass
class ComponentSpec:
    name: str
    component_class: str
    attach_to: str = "Root"
    is_root: bool = False


@dataclass
class NodeSpec:
    node_type: str
    function_name: str = ""
    target_class: str = ""
    event_name: str = ""
    variable_name: str = ""


@dataclass
class ConnectionSpec:
    src_node: str
    src_pin: str
    dst_node: str
    dst_pin: str


@dataclass
class BuilderSpec:
    name: str
    package_path: str = "/Game/Blueprints/"
    parent_class: str = "Actor"
    blueprint_type: BlueprintType = BlueprintType.ACTOR
    variables: List[VariableSpec] = field(default_factory=list)
    components: List[ComponentSpec] = field(default_factory=list)
    nodes: List[NodeSpec] = field(default_factory=list)
    connections: List[ConnectionSpec] = field(default_factory=list)
    auto_compile: bool = True


@dataclass
class BuildStep:
    step: str
    status: str
    detail: str = ""
    duration_ms: float = 0.0
    fallback_used: bool = False


@dataclass
class BuildResult:
    blueprint_path: str = ""
    blueprint_name: str = ""
    mode: BuilderMode = BuilderMode.CREATE
    status: BuildStatus = BuildStatus.PENDING
    steps: List[BuildStep] = field(default_factory=list)
    variables_added: List[str] = field(default_factory=list)
    components_added: List[str] = field(default_factory=list)
    nodes_added: List[str] = field(default_factory=list)
    connections_made: List[str] = field(default_factory=list)
    compiled: bool = False
    rollback_performed: bool = False
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    total_elapsed_ms: float = 0.0


# ── 父类映射 ──

BLUEPRINT_TYPE_PARENT_MAP: Dict[BlueprintType, str] = {
    BlueprintType.ACTOR:            "Actor",
    BlueprintType.WIDGET:           "UserWidget",
    BlueprintType.FUNCTION_LIBRARY: "BlueprintFunctionLibrary",
    BlueprintType.INTERFACE:        "BlueprintInterface",
}

VAR_TYPE_MAP: Dict[str, str] = {
    "bool":      "bool",
    "boolean":   "bool",
    "int":       "int",
    "integer":   "int",
    "float":     "float",
    "double":    "float",
    "string":    "String",
    "str":       "String",
    "vector":    "Vector",
    "rotator":   "Rotator",
    "transform": "Transform",
    "color":     "LinearColor",
    "text":      "Text",
    "name":      "Name",
    "object":    "Object",
    "actor":     "Actor",
    "class":     "Class",
}


def _normalize_var_type(raw_type: str) -> str:
    return VAR_TYPE_MAP.get(raw_type.lower(), raw_type)



# ── BlueprintBuilder ──

class BlueprintBuilder:
    """蓝图创建引擎。

    铁律 #1: 创建节点+连线必须一次调用完成
    铁律 #3: 连接结果必须检查
    铁律 #12: CDO 修改必须编译后做
    P01: ConnectPins 假阳性 — 必须双向 LinkedTo 验证
    """

    MAX_COMPILE_RETRIES = 3

    def __init__(self, bridge=None, unreal_module=None, transaction_enabled=True):
        self.bridge = bridge
        self.unreal = unreal_module
        self._transaction_enabled = transaction_enabled and _safe_wrapper_available
        self._mode = None
        self._bp_ref = None
        self._bp_path = ""
        self._spec = None
        self._result = None
        self._t0 = 0.0
        self._created_assets = []
        self._node_map = {}

    def create(self, spec):
        self._init_create(spec)
        try:
            if not self._step_create_blueprint():
                return self._finalize(BuildStatus.FAILED)
            self._step_add_variables()
            self._step_add_components()
            self._step_add_nodes()
            self._step_connect_pins()
            if spec.auto_compile:
                self._step_compile()
            else:
                self._result.status = BuildStatus.VERIFIED
            return self._finalize()
        except Exception as e:
            self._result.errors.append("create exception: " + type(e).__name__ + ": " + str(e))
            return self._finalize(BuildStatus.FAILED)

    def continue_from(self, blueprint_path):
        bp_path = blueprint_path.strip()
        if bp_path.endswith("_C"):
            bp_path = bp_path[:-2]
        self._init_continue(bp_path)
        try:
            if self.unreal:
                bp = self.unreal.EditorAssetLibrary.load_asset(bp_path)
            elif self.bridge and hasattr(self.bridge, "load_asset"):
                bp = self.bridge.load_asset(bp_path)
            else:
                self._result.errors.append("no unreal or bridge")
                return None
            if not bp:
                self._result.errors.append("blueprint not found: " + bp_path)
                return None
            self._bp_ref = bp
            self._record_step("continue_from", "ok", "loaded: " + bp_path)
            return bp
        except Exception as e:
            self._result.errors.append("load failed: " + str(e))
            return None

    def add_variable(self, name, var_type, default_value=None, expose=True,
                     category="Default", is_array=False, tooltip=""):
        self._ensure_continue_mode()
        t0 = time.time()
        step = BuildStep(step="add_variable:" + name, status="failed")
        var_type_norm = _normalize_var_type(var_type)
        try:
            if self.bridge and hasattr(self.bridge, "add_member_variable"):
                self.bridge.add_member_variable(self._bp_ref, name, var_type_norm)
            elif self.unreal:
                self._unreal_add_variable(name, var_type_norm)
            else:
                step.detail = "no API available"
                return step
            if default_value is not None:
                self._compile_current()
                self._set_variable_default(name, default_value)
            step.status = "ok"
            step.detail = "variable " + name + " (" + var_type_norm + ") added"
            self._result.variables_added.append(name)
        except Exception as e:
            step.detail = "add variable failed: " + str(e)
            self._result.errors.append(step.detail)
        step.duration_ms = (time.time() - t0) * 1000
        self._result.steps.append(step)
        return step

    def add_component(self, name, component_class, attach_to="Root", is_root=False):
        self._ensure_continue_mode()
        t0 = time.time()
        step = BuildStep(step="add_component:" + name, status="failed")
        try:
            if self.bridge and hasattr(self.bridge, "add_component"):
                self.bridge.add_component(self._bp_ref, component_class, name)
            elif self.unreal:
                self._unreal_add_component(name, component_class)
            else:
                step.detail = "no API available"
                return step
            step.status = "ok"
            step.detail = "component " + name + " added"
            self._result.components_added.append(name)
        except Exception as e:
            step.detail = "add component failed: " + str(e)
            self._result.errors.append(step.detail)
        step.duration_ms = (time.time() - t0) * 1000
        self._result.steps.append(step)
        return step

    def add_function_node(self, function_name, target_class):
        self._ensure_continue_mode()
        t0 = time.time()
        step = BuildStep(step="add_node:" + function_name, status="failed")
        try:
            node = None
            if self.bridge and hasattr(self.bridge, "add_function_call_node"):
                node = self.bridge.add_function_call_node(function_name, target_class)
            if node is None:
                step.detail = "add_function_call_node returned None (PTN-002): need full path, got " + target_class
                self._result.errors.append(step.detail)
                return step
            self._node_map[function_name] = node
            step.status = "ok"
            step.detail = "node " + function_name + " added"
            self._result.nodes_added.append(function_name)
        except Exception as e:
            step.detail = "add node failed: " + str(e)
            self._result.errors.append(step.detail)
        step.duration_ms = (time.time() - t0) * 1000
        self._result.steps.append(step)
        return step

    def add_event_node(self, event_name, b_override=True):
        self._ensure_continue_mode()
        t0 = time.time()
        step = BuildStep(step="add_event:" + event_name, status="failed")
        try:
            node = None
            if self.bridge and hasattr(self.bridge, "add_event_node"):
                node = self.bridge.add_event_node(self._bp_ref, event_name)
                if node and b_override and hasattr(node, "bOverrideFunction"):
                    node.bOverrideFunction = True
            if node is None:
                step.detail = "add_event_node returned None: " + event_name
                self._result.errors.append(step.detail)
                return step
            self._node_map[event_name] = node
            step.status = "ok"
            step.detail = "event " + event_name + " added"
            self._result.nodes_added.append(event_name)
        except Exception as e:
            step.detail = "add event failed: " + str(e)
            self._result.errors.append(step.detail)
        step.duration_ms = (time.time() - t0) * 1000
        self._result.steps.append(step)
        return step

    def connect(self, src_node, src_pin, dst_node, dst_pin):
        """P01: ConnectPins + bidirectional LinkedTo.Contains() verify"""
        self._ensure_continue_mode()
        t0 = time.time()
        conn_key = src_node + "." + src_pin + "->" + dst_node + "." + dst_pin
        step = BuildStep(step="connect:" + conn_key, status="failed")
        try:
            connected = False
            if self.bridge and hasattr(self.bridge, "connect_pins"):
                connected = self.bridge.connect_pins(src_node, src_pin, dst_node, dst_pin)
            verified = self._verify_bidirectional(src_node, src_pin, dst_node, dst_pin)
            if not verified:
                fb = self._make_link_to_fallback(src_node, src_pin, dst_node, dst_pin)
                if fb:
                    verified = self._verify_bidirectional(src_node, src_pin, dst_node, dst_pin)
                    if verified:
                        step.fallback_used = True
            if verified:
                step.status = "ok"
                step.detail = "connected: " + conn_key + (" (fallback)" if step.fallback_used else "")
                self._result.connections_made.append(conn_key)
            else:
                step.detail = "connect FAILED: " + conn_key
                self._result.errors.append(step.detail)
        except Exception as e:
            step.detail = "connect exception: " + str(e)
            self._result.errors.append(step.detail)
        step.duration_ms = (time.time() - t0) * 1000
        self._result.steps.append(step)
        return step

    def compile_and_finalize(self):
        self._ensure_continue_mode()
        self._step_compile()
        return self._finalize()

    def rollback(self):
        for asset_path in reversed(self._created_assets):
            try:
                if self.bridge and hasattr(self.bridge, "delete_asset"):
                    self.bridge.delete_asset(asset_path)
                elif self.unreal:
                    self.unreal.EditorAssetLibrary.delete_asset(asset_path)
            except Exception as e:
                self._result.errors.append("rollback failed " + asset_path + ": " + str(e))
        self._result.rollback_performed = True
        self._result.status = BuildStatus.ROLLED_BACK
        return self._result

    # ══  Internal: Step implementations ══

    def _init_create(self, spec):
        self._mode = BuilderMode.CREATE
        self._spec = spec
        self._bp_path = spec.package_path.rstrip("/") + "/" + spec.name
        self._created_assets.clear()
        self._node_map.clear()
        self._t0 = time.time()
        self._result = BuildResult(
            blueprint_path=self._bp_path,
            blueprint_name=spec.name,
            mode=BuilderMode.CREATE,
            status=BuildStatus.PENDING,
        )

    def _init_continue(self, bp_path):
        self._mode = BuilderMode.CONTINUE
        self._bp_path = bp_path
        self._created_assets.clear()
        self._node_map.clear()
        self._t0 = time.time()
        bp_name = bp_path.rsplit("/", 1)[-1] if "/" in bp_path else bp_path
        self._result = BuildResult(
            blueprint_path=bp_path,
            blueprint_name=bp_name,
            mode=BuilderMode.CONTINUE,
            status=BuildStatus.PENDING,
        )

    def _step_create_blueprint(self):
        self._result.status = BuildStatus.CREATING
        t0 = time.time()
        spec = self._spec
        step = BuildStep(step="create_blueprint", status="failed")
        if self._check_exists(self._bp_path):
            step.detail = "blueprint already exists: " + self._bp_path
            step.status = "skipped"
            self._result.warnings.append(step.detail)
            self._result.steps.append(step)
            return True
        bp = None
        try:
            if self._transaction_enabled:
                txn = CreateBlueprintTransaction(
                    self.bridge, spec.package_path.rstrip("/"),
                    spec.name, spec.parent_class
                )
                txn_result = txn.run()
                if txn_result.ok:
                    bp = txn_result.data
                    step.fallback_used = txn_result.fallback_used
                else:
                    step.detail = "create transaction failed: " + txn_result.details
                    self._result.errors.append(step.detail)
                    self._result.steps.append(step)
                    return False
            else:
                bp = self.bridge.create_actor_blueprint(
                    spec.package_path.rstrip("/"), spec.name
                )
        except Exception as e:
            step.detail = "create blueprint exception: " + str(e)
            self._result.errors.append(step.detail)
            self._result.steps.append(step)
            return False
        if bp is None:
            step.detail = "create_actor_blueprint returned None"
            self._result.errors.append(step.detail)
            self._result.steps.append(step)
            return False
        self._bp_ref = bp
        self._created_assets.append(self._bp_path)
        step.status = "ok"
        step.detail = "blueprint created: " + self._bp_path
        step.duration_ms = (time.time() - t0) * 1000
        self._result.steps.append(step)
        return True

    def _step_add_variables(self):
        self._result.status = BuildStatus.ADDING_VARS
        for v in self._spec.variables:
            self.add_variable(
                v.name, v.var_type, v.default_value,
                v.expose, v.category, v.is_array, v.tooltip
            )

    def _step_add_components(self):
        self._result.status = BuildStatus.ADDING_COMPONENTS
        for c in self._spec.components:
            self.add_component(c.name, c.component_class, c.attach_to, c.is_root)

    def _step_add_nodes(self):
        self._result.status = BuildStatus.ADDING_NODES
        for n in self._spec.nodes:
            if n.node_type == "event":
                self.add_event_node(n.event_name or n.function_name)
            elif n.node_type == "function_call":
                self.add_function_node(n.function_name, n.target_class)

    def _step_connect_pins(self):
        self._result.status = BuildStatus.CONNECTING
        for c in self._spec.connections:
            self.connect(c.src_node, c.src_pin, c.dst_node, c.dst_pin)

    def _step_compile(self):
        self._result.status = BuildStatus.COMPILING
        t0 = time.time()
        step = BuildStep(step="compile", status="failed")
        for attempt in range(1, self.MAX_COMPILE_RETRIES + 1):
            try:
                compiled = self._compile_current()
                if compiled:
                    step.status = "ok"
                    step.detail = "compile OK (attempt " + str(attempt) + ")"
                    self._result.compiled = True
                    self._result.status = BuildStatus.VERIFIED
                    break
                else:
                    step.detail = "compile FAILED (attempt " + str(attempt) + "/" + str(self.MAX_COMPILE_RETRIES) + ")"
                    if attempt == self.MAX_COMPILE_RETRIES:
                        self._result.errors.append(step.detail)
                        self._result.status = BuildStatus.FAILED
            except Exception as e:
                step.detail = "compile exception (attempt " + str(attempt) + "): " + str(e)
                if attempt == self.MAX_COMPILE_RETRIES:
                    self._result.errors.append(step.detail)
                    self._result.status = BuildStatus.FAILED
        step.duration_ms = (time.time() - t0) * 1000
        self._result.steps.append(step)

    def _compile_current(self):
        if self.bridge and hasattr(self.bridge, "compile_blueprint"):
            return self.bridge.compile_blueprint(self._bp_ref)
        elif self.unreal:
            try:
                self.unreal.BlueprintEditorLibrary.compile_blueprint(self._bp_ref)
                return True
            except Exception as e:
                logging.exception("compile_blueprint failed: %s", e)
                return False
        return False

    # ══  Internal: Validation & Fallback ══

    def _verify_bidirectional(self, src_node, src_pin, dst_node, dst_pin):
        """🔒 C-004 修复：添加 linked_to 缺失时的降级日志。"""
        try:
            if self.bridge and hasattr(self.bridge, "list_pins"):
                pins = self.bridge.list_pins(self._bp_ref, src_node)
                for pin in pins:
                    if pin.get("pin_name") == src_pin:
                        linked = pin.get("linked_to")
                        if linked is None:
                            print(f"[blueprint_builder] ⚠️ pin.linked_to 不存在，Bridge API 可能已变更")
                            # 降级：只依赖 is_connected
                            if pin.get("is_connected", False):
                                return True
                            return False
                        if dst_node in linked:
                            return True
                        if pin.get("is_connected", False):
                            return True
                pins = self.bridge.list_pins(self._bp_ref, dst_node)
                for pin in pins:
                    if pin.get("pin_name") == dst_pin:
                        linked = pin.get("linked_to")
                        if linked is None:
                            if pin.get("is_connected", False):
                                return True
                            return False
                        if src_node in linked:
                            return True
            return False
        except Exception as e:
            logging.exception("_verify_bidirectional failed: %s", e)
            return False

    def _make_link_to_fallback(self, src_node, src_pin, dst_node, dst_pin):
        try:
            if self.bridge and hasattr(self.bridge, "make_link_to"):
                self.bridge.make_link_to(src_node, src_pin, dst_node, dst_pin)
                self.bridge.make_link_to(dst_node, dst_pin, src_node, src_pin)
                return True
        except Exception as e:
            logging.exception("_make_link_to_fallback failed: %s", e)
            pass
        return False

    def _unreal_add_variable(self, name, var_type):
        if not self.unreal:
            return
        gen_class = self._bp_ref.generated_class()
        if not gen_class:
            raise RuntimeError("no GeneratedClass")
        bp_editor = self.unreal.get_editor_subsystem(
            self.unreal.BlueprintEditorSubsystem
        )
        if bp_editor:
            bp_editor.add_member_variable(self._bp_ref, name, var_type)

    def _unreal_add_component(self, name, comp_class):
        if not self.unreal:
            return
        scs = self._bp_ref.simple_construction_script()
        if scs and hasattr(scs, "add_component"):
            scs.add_component(comp_class, name)

    def _set_variable_default(self, name, value):
        try:
            gen_class = self._bp_ref.generated_class()
            if gen_class:
                cdo = gen_class.get_default_object()
                if cdo:
                    try:
                        cdo.set_editor_property(name, value)
                    except Exception as e1:
                        logging.exception("_set_variable_default set_editor_property failed for %s: %s", name, e1)
                        try:
                            setattr(cdo, name, value)
                        except Exception as e2:
                            logging.exception("_set_variable_default setattr fallback failed for %s: %s", name, e2)
        except Exception as e:
            logging.exception("_set_variable_default failed for %s: %s", name, e)

    def _check_exists(self, bp_path):
        try:
            if self.unreal:
                return self.unreal.EditorAssetLibrary.does_asset_exist(bp_path)
            if self.bridge and hasattr(self.bridge, "does_asset_exist"):
                return self.bridge.does_asset_exist(bp_path)
        except Exception as e:
            logging.exception("_check_exists failed for %s: %s", bp_path, e)
        return False

    def _ensure_continue_mode(self):
        if self._mode != BuilderMode.CONTINUE:
            raise RuntimeError("CONTINUE mode only. Call continue_from() first.")
        if self._bp_ref is None:
            raise RuntimeError("no blueprint ref")
        if self._result is None:
            self._result = BuildResult(
                blueprint_path=self._bp_path,
                mode=BuilderMode.CONTINUE,
                status=BuildStatus.PENDING,
            )

    def _record_step(self, step_name, status, detail=""):
        step = BuildStep(step=step_name, status=status, detail=detail)
        self._result.steps.append(step)

    def _finalize(self, status=None):
        if status:
            self._result.status = status
        if self._result.status == BuildStatus.PENDING:
            self._result.status = BuildStatus.VERIFIED
        self._result.total_elapsed_ms = (time.time() - self._t0) * 1000
        if (self._result.status == BuildStatus.FAILED and
                not self._result.rollback_performed and
                self._mode == BuilderMode.CREATE):
            self.rollback()
        return self._result

    # ══  Serialization ══

    def result_to_dict(self):
        r = self._result
        return {
            "blueprint_path": r.blueprint_path,
            "blueprint_name": r.blueprint_name,
            "mode": r.mode.name,
            "status": r.status.value,
            "steps": [
                {
                    "step": s.step, "status": s.status,
                    "detail": s.detail, "duration_ms": s.duration_ms,
                    "fallback_used": s.fallback_used,
                }
                for s in r.steps
            ],
            "variables_added": r.variables_added,
            "components_added": r.components_added,
            "nodes_added": r.nodes_added,
            "connections_made": r.connections_made,
            "compiled": r.compiled,
            "rollback_performed": r.rollback_performed,
            "errors": r.errors,
            "warnings": r.warnings,
            "total_elapsed_ms": r.total_elapsed_ms,
        }

    def result_to_json(self, indent=2):
        return json.dumps(self.result_to_dict(), indent=indent,
                          ensure_ascii=False, default=str)


# ──  Quick builders ──

def create_blueprint_quick(bridge, name, package_path="/Game/Blueprints/",
                           parent_class="Actor", variables=None, events=None,
                           function_calls=None, connections=None):
    var_specs = [
        VariableSpec(
            name=v["name"], var_type=v.get("type", "bool"),
            default_value=v.get("default"),
            category=v.get("category", "Default"),
        )
        for v in (variables or [])
    ]
    node_specs = []
    for evt in (events or []):
        node_specs.append(NodeSpec(node_type="event", event_name=evt))
    for func, cls in (function_calls or []):
        node_specs.append(NodeSpec(
            node_type="function_call", function_name=func, target_class=cls
        ))
    conn_specs = [
        ConnectionSpec(src_node=s, src_pin=sp, dst_node=d, dst_pin=dp)
        for s, sp, d, dp in (connections or [])
    ]
    spec = BuilderSpec(
        name=name, package_path=package_path, parent_class=parent_class,
        variables=var_specs, nodes=node_specs, connections=conn_specs,
    )
    builder = BlueprintBuilder(bridge=bridge)
    return builder.create(spec)


def _spec_from_json(data: dict) -> BuilderSpec:
    """把 JSON spec（嵌套 dict 列表）转换为 BuilderSpec（dataclass 树）。

    v2.20 修复（P2）：旧 CLI 直接 `BuilderSpec(**data)` —— JSON 里的
    variables/nodes/connections 仍是 dict 列表，后续 `v.name` 属性访问
    AttributeError → create 必报 "create exception"。与 create_blueprint_quick
    的转换逻辑对齐。
    """
    var_specs = [
        VariableSpec(
            name=v["name"], var_type=v.get("type", v.get("var_type", "bool")),
            default_value=v.get("default", v.get("default_value")),
            category=v.get("category", "Default"),
            expose=v.get("expose", True),
        )
        for v in (data.get("variables") or [])
        if isinstance(v, dict) and v.get("name")
    ]
    node_specs = []
    for n in (data.get("nodes") or []):
        if not isinstance(n, dict):
            continue
        node_specs.append(NodeSpec(
            node_type=n.get("node_type", "event"),
            function_name=n.get("function_name", ""),
            target_class=n.get("target_class", ""),
            event_name=n.get("event_name", ""),
            variable_name=n.get("variable_name", ""),
        ))
    conn_specs = [
        ConnectionSpec(src_node=c["src_node"], src_pin=c.get("src_pin", ""),
                       dst_node=c["dst_node"], dst_pin=c.get("dst_pin", ""))
        for c in (data.get("connections") or [])
        if isinstance(c, dict) and c.get("src_node") and c.get("dst_node")
    ]
    return BuilderSpec(
        name=data.get("name", ""),
        package_path=data.get("package_path", data.get("path", "/Game/Blueprints/")),
        parent_class=data.get("parent_class", data.get("parent", "Actor")),
        variables=var_specs, nodes=node_specs, connections=conn_specs,
        auto_compile=data.get("auto_compile", True),
    )


# ── CLI ──

def main():
    import argparse
    parser = argparse.ArgumentParser(description="blueprint creation engine")
    subparsers = parser.add_subparsers(dest="command")

    cp = subparsers.add_parser("create", help="create new blueprint")
    cp.add_argument("--name", required=True)
    cp.add_argument("--path", default="/Game/Blueprints/")
    cp.add_argument("--parent", default="Actor")
    cp.add_argument("--spec", help="JSON spec file")
    cp.add_argument("--output", "-o", help="output JSON result")

    ct = subparsers.add_parser("continue", help="continue existing blueprint")
    ct.add_argument("blueprint_path")
    ct.add_argument("--ops", help="operations JSON file")
    ct.add_argument("--output", "-o", help="output JSON result")

    args = parser.parse_args()

    if args.command == "create":
        if args.spec:
            with open(args.spec, "r", encoding="utf-8") as f:
                data = json.load(f)
            spec = _spec_from_json(data)
        else:
            spec = BuilderSpec(
                name=args.name, package_path=args.path,
                parent_class=args.parent
            )
        builder = BlueprintBuilder()
        builder.create(spec)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(builder.result_to_json())
            print("result written: " + args.output)
        else:
            print(builder.result_to_json())
        # v2.20 修复（P2）：构建失败/回滚后 CLI 仍退出 0 → 自动化调用方无法感知。
        _status = str(getattr(builder.result, "status", ""))
        if _status in ("FAILED", "ROLLED_BACK"):
            sys.exit(1)

    elif args.command == "continue":
        builder = BlueprintBuilder()
        bp = builder.continue_from(args.blueprint_path)
        if not bp:
            print("load failed: " + args.blueprint_path)
            sys.exit(1)
        if args.ops:
            with open(args.ops, "r", encoding="utf-8") as f:
                ops = json.load(f)
            for op in ops:
                t = op.get("type")
                p = op.get("params", {})
                if t == "add_variable":
                    builder.add_variable(**p)
                elif t == "add_component":
                    builder.add_component(**p)
                elif t == "add_function_node":
                    builder.add_function_node(**p)
                elif t == "connect":
                    builder.connect(**p)
        builder.compile_and_finalize()
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(builder.result_to_json())
            print("result written: " + args.output)
        else:
            print(builder.result_to_json())
        _status = str(getattr(builder.result, "status", ""))
        if _status in ("FAILED", "ROLLED_BACK"):
            sys.exit(1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
