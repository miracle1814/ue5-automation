#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
blueprint_reader.py — UE5 蓝图逆向识别引擎
============================================
ue5-automation Skill 核心模块。读取蓝图拓扑（节点/变量/连线/引用），
产出结构化 BlueprintAnalysis。

三级降级策略：
  L1: UE 原生 Python API（EditorAssetLibrary + KismetSystemLibrary）
  L2: C++ BlueprintPythonBridge（GetNodeConnections / GetPinInfos / ListNodes）
  L3: 最低可用读取（仅节点标题 + 类型，无连线）

输出格式适配 7 步交付物：
  0. 前置检查 → 1. 基本信息 → 2. 变量清单 → 3. 节点+连线拓扑
  → 4. 流程图 → 5. 引用资产 → 6. 关联蓝图 → 7. 汇总

🔴 已知教训（来自 KB）：
  - ConnectPins 假阳性 → 读连线用 GetNodeConnections 验证，不能只信 ConnectPins 返回值
  - CDO 编译时序 → 变量默认值在编译后读取
  - 跨 ue_send 调用引用丢失 → 全部读取在一次调用中完成
  - 蓝图编译失败 → 原地诊断修复，不新建蓝图逃避

作者：dev（代码开发 Agent）
日期：2026-07-22
"""

import json
import os
import sys
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, asdict
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from pathlib import Path


# ═══════════════════════════════════════════════════════════
# 路径配置（A-005 修复：消除硬编码绝对路径）
# ═══════════════════════════════════════════════════════════

_WORKSPACE = os.environ.get(
    "COPAW_WORKSPACE",
    os.path.join(os.path.expanduser("~"), ".copaw", "workspaces", "default")
)
SKILL_DIR = os.path.join(_WORKSPACE, "skills", "ue5-automation")
SCRIPTS_DIR = os.path.join(SKILL_DIR, "scripts")
OUTPUT_DIR = os.path.join(_WORKSPACE, "output")

# T-20260915-UE5PKG-STANDALONE：脚本自身目录（发布包/独立部署布局下
# ue_send.py 与 blueprint_reader.py 同处 <pkg>\skill\ue5-automation\scripts\）。
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ue_send.py 路径（多级 fallback，不硬编码）
def _find_ue_send() -> Optional[str]:
    """查找 ue_send.py，返回第一个存在的路径。
    检测顺序：
      ① 环境变量 UE_SEND_PATH
      ② 脚本自身目录（发布包/独立部署布局）
      ③ workspace/skills/ue5-automation/scripts（工作区布局）
      ④ WORKSPACE 下递归搜索（最多 3 层）
    """
    # ① 环境变量
    env = os.environ.get("UE_SEND_PATH", "")
    if env and os.path.isfile(env):
        return env

    # ② 脚本自身目录（发布包/独立部署：ue_send.py 与 blueprint_reader.py 同级）
    here = os.path.join(_SCRIPT_DIR, "ue_send.py")
    if os.path.isfile(here):
        return here

    # ③ 工作区布局
    local = os.path.join(SCRIPTS_DIR, "ue_send.py")
    if os.path.isfile(local):
        return local

    # ③ WORKSPACE 下递归搜索
    for root, dirs, files in os.walk(_WORKSPACE):
        depth = root.replace(_WORKSPACE, "").count(os.sep)
        if depth > 3:
            dirs.clear()
            continue
        if "ue_send.py" in files:
            return os.path.join(root, "ue_send.py")

    return None


# ═══════════════════════════════════════════════════════════
# 数据类：分析结果
# ═══════════════════════════════════════════════════════════


class DegradeLevel(Enum):
    """降级等级"""
    L1_NATIVE = auto()    # UE 原生 Python API
    L2_BRIDGE = auto()    # C++ Bridge
    L3_MINIMAL = auto()   # 最低可用


@dataclass
class PinInfo:
    """引脚信息"""
    name: str
    direction: str          # "Input" / "Output"
    pin_type: str = ""      # 引脚类型（object/int/float/exec...）
    is_exec: bool = False
    is_connected: bool = False
    pin_id: str = ""
    default_value: str = ""
    sub_category: str = ""


@dataclass
class NodeInfo:
    """节点信息"""
    title: str
    node_class: str         # 例如 K2Node_Event, K2Node_CallFunction
    pos_x: int = 0
    pos_y: int = 0
    node_guid: str = ""
    graph_name: str = ""
    pins: List[PinInfo] = field(default_factory=list)
    is_event: bool = False
    is_pure: bool = False
    is_latent: bool = False
    function_name: str = ""  # 对于 CallFunction 节点
    target_class: str = ""   # 对于 CallFunction 节点


@dataclass
class ConnectionInfo:
    """连线信息"""
    from_node: str
    from_pin: str
    from_direction: str = ""
    from_guid: str = ""
    to_node: str = ""
    to_pin: str = ""
    to_direction: str = ""
    to_guid: str = ""


@dataclass
class GraphAnalysis:
    """单个 Graph 的分析结果"""
    graph_name: str
    graph_type: str = ""           # "EventGraph" / "FunctionGraph" / "ConstructionScript"
    node_count: int = 0
    nodes: List[NodeInfo] = field(default_factory=list)
    connections: List[ConnectionInfo] = field(default_factory=list)


@dataclass
class BlueprintVariable:
    """蓝图变量"""
    var_name: str
    var_type: str
    default_value: str = ""
    category: str = "Default"
    is_exposed: bool = True
    is_array: bool = False
    is_editable: bool = True
    tooltip: str = ""


@dataclass
class AssetReference:
    """引用资产"""
    asset_path: str
    asset_type: str              # "CastTo" / "CreateWidget" / "SpawnActor" / "LoadObject"
    source_node: str = ""
    is_suspicious: bool = False  # 路径不匹配/文件缺失
    note: str = ""


@dataclass
class BlueprintBasicInfo:
    """蓝图基本信息"""
    name: str
    asset_path: str
    parent_class: str
    blueprint_type: str = ""     # "Blueprint" / "WidgetBlueprint" / "AnimBlueprint"
    interfaces: List[str] = field(default_factory=list)
    last_modified: str = ""
    node_count_total: int = 0
    graph_count: int = 0


@dataclass
class BlueprintAnalysis:
    """蓝图完整分析结果"""
    basic_info: BlueprintBasicInfo
    variables: List[BlueprintVariable] = field(default_factory=list)
    graphs: Dict[str, GraphAnalysis] = field(default_factory=dict)
    connections: List[ConnectionInfo] = field(default_factory=list)
    referenced_assets: List[AssetReference] = field(default_factory=list)
    related_blueprints: List[str] = field(default_factory=list)
    degrade_level: DegradeLevel = DegradeLevel.L1_NATIVE
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    analysis_time: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """转为可 JSON 序列化的字典。"""
        d = {
            "basic_info": asdict(self.basic_info),
            "variables": [asdict(v) for v in self.variables],
            "graphs": {
                name: asdict(g) for name, g in self.graphs.items()
            },
            "connections": [asdict(c) for c in self.connections],
            "referenced_assets": [asdict(r) for r in self.referenced_assets],
            "related_blueprints": self.related_blueprints,
            "degrade_level": self.degrade_level.name,
            "errors": self.errors,
            "warnings": self.warnings,
            "analysis_time": self.analysis_time,
        }
        return d

    def to_json(self, indent: int = 2) -> str:
        """转为 JSON 字符串。"""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
# UE 内部探针代码（作为字符串模板，通过 ue_send 注入）
# ═══════════════════════════════════════════════════════════

# L1 探针：使用 UE 原生 Python API
PROBE_L1 = r'''
import json
import unreal

bp_path = {blueprint_path}

result = {{
    "ok": False,
    "level": "L1",
    "basic_info": {{}},
    "variables": [],
    "graphs": {{}},
    "connections": [],
    "errors": [],
}}

try:
    # ── 加载蓝图 ──
    bp = unreal.EditorAssetLibrary.load_asset(bp_path)
    if not bp:
        result["errors"].append(f"加载失败: {bp_path}")
        print("__JSON_RESULT__" + json.dumps(result, ensure_ascii=False) + "__JSON_END__")
        raise SystemExit(0)

    # 如果是 BlueprintGeneratedClass，尝试取 ClassDefaultObject 回推
    try:
        bp_class = bp.get_class().get_name()
    except:
        bp_class = "Unknown"

    try:
        if "BlueprintGeneratedClass" in bp_class:
            cdo = unreal.get_default_object(bp)
            if cdo:
                bp = cdo.get_class().get_default_object().get_class().class_generated_by
    except:
        pass

    # ── 基本信息 ──
    try:
        parent = bp.get_editor_property("parent_class") or bp.parent_class()
    except:
        try:
            parent = bp.parent_class()
        except:
            parent = None

    parent_name = parent.get_name() if parent else "Unknown"

    try:
        interfaces = bp.get_editor_property("implemented_interfaces")
        interface_names = [i.get_name() for i in interfaces] if interfaces else []
    except:
        interface_names = []

    try:
        bp_type = bp.get_class().get_name()
    except:
        bp_type = "Blueprint"

    result["basic_info"] = {{
        "name": bp.get_name(),
        "asset_path": bp_path,
        "parent_class": parent_name,
        "blueprint_type": bp_type,
        "interfaces": interface_names,
    }}

    # ── 变量提取 ──
    try:
        new_vars = bp.get_editor_property("new_variables")
        for var in (new_vars or []):
            try:
                vn = var.get_editor_property("var_name")
                vt = var.get_editor_property("var_type")
                cat = var.get_editor_property("category") or "Default"
                result["variables"].append({{
                    "var_name": str(vn),
                    "var_type": str(vt),
                    "category": str(cat),
                    "default_value": "",
                    "is_exposed": True,
                    "is_array": False,
                }})
            except:
                pass
    except:
        result["warnings"] = result.get("warnings", []) + ["变量提取失败"]

    # ── 遍历所有 Graph ──
    try:
        all_graphs = unreal.BlueprintEditorLibrary.get_all_graphs(bp)
    except:
        try:
            all_graphs = []
            # 尝试逐个获取已知图类型
            for getter in [
                lambda: unreal.BlueprintEditorLibrary.get_event_graph(bp),
                lambda: unreal.BlueprintEditorLibrary.get_construction_script_graph(bp),
            ]:
                try:
                    g = getter()
                    if g:
                        all_graphs.append(g)
                except:
                    pass
            # 尝试函数图
            try:
                func_graphs = unreal.BlueprintEditorLibrary.get_function_graphs(bp)
                if func_graphs:
                    all_graphs.extend(func_graphs)
            except:
                pass
        except:
            all_graphs = []

    graph_count = 0
    node_count = 0

    for graph in (all_graphs or []):
        try:
            gname = graph.get_name()
        except:
            continue

        try:
            nodes = unreal.BlueprintEditorLibrary.get_all_graph_nodes(graph)
        except:
            try:
                nodes = graph.Nodes
            except:
                nodes = []

        graph_nodes = []
        for node in (nodes or []):
            try:
                nc = node.get_class().get_name()
                nt = node.get_name()
            except:
                continue

            node_count += 1
            pins_list = []
            try:
                raw_pins = unreal.KismetSystemLibrary.get_pins(node)
            except:
                try:
                    raw_pins = node.Pins
                except:
                    raw_pins = []

            for pin in (raw_pins or []):
                try:
                    pn = pin.get_name()
                    pd = str(pin.direction) if hasattr(pin, 'direction') else "Input"
                    try:
                        pt = str(pin.pin_type.pin_category) if hasattr(pin, 'pin_type') else ""
                    except:
                        pt = ""
                    # linked_to
                    linked = []
                    try:
                        linked = unreal.KismetSystemLibrary.get_linked_pins(pin)
                    except:
                        try:
                            linked = pin.linked_to
                        except:
                            pass
                    pins_list.append({{
                        "name": pn,
                        "direction": pd,
                        "pin_type": pt,
                        "is_connected": len(linked) > 0 if linked else False,
                    }})
                except:
                    pass

            graph_nodes.append({{
                "title": nt,
                "node_class": nc,
                "pins": pins_list,
                "is_event": nc.startswith("K2Node_Event"),
                "is_pure": nc.endswith("_Pure") if hasattr(node, 'is_pure') else False,
            }})

        graph_count += 1
        result["graphs"][gname] = {{
            "graph_name": gname,
            "node_count": len(graph_nodes),
            "nodes": graph_nodes,
        }}

    result["basic_info"]["node_count_total"] = node_count
    result["basic_info"]["graph_count"] = graph_count
    result["ok"] = True

except Exception as e:
    result["errors"].append(f"L1 探针异常: {str(e)}")

# 输出结果到 stdout（ue_send 会捕获）
print("__JSON_RESULT__" + json.dumps(result, ensure_ascii=False, default=str) + "__JSON_END__")
'''


# L2 探针：使用 C++ Bridge API
PROBE_L2 = r'''
import json
import unreal

bp_path = {blueprint_path}

result = {{
    "ok": False,
    "level": "L2",
    "basic_info": {{}},
    "variables": [],
    "graphs": {{}},
    "connections": [],
    "errors": [],
}}

try:
    bridge = unreal.BlueprintPythonBridge
    _ = dir(bridge)  # 快速探测是否可用
except AttributeError:
    result["errors"].append("L2 Bridge 未安装或未加载")
    print("__JSON_RESULT__" + json.dumps(result, ensure_ascii=False) + "__JSON_END__")
    raise SystemExit(0)

try:
    bp = unreal.EditorAssetLibrary.load_asset(bp_path)
    if not bp:
        result["errors"].append(f"加载失败: {bp_path}")
        print("__JSON_RESULT__" + json.dumps(result, ensure_ascii=False) + "__JSON_END__")
        raise SystemExit(0)

    # 基本信息（UE5.1 实测：Blueprint 未暴露 parent_class → 走 AssetRegistry 标签）
    parent_name = "Unknown"
    bp_type = "Blueprint"
    try:
        _ad = unreal.EditorAssetLibrary.find_asset_data(bp_path)
        _raw_parent = ""
        for _tag in ("ParentClass", "NativeParentClass"):
            try:
                _val = _ad.get_tag_value(_tag)
            except:
                _val = None
            if _val and str(_val) != "None":
                _raw_parent = str(_val)
                break
        if _raw_parent:
            _tail = _raw_parent.split("'")[-2] if "'" in _raw_parent else _raw_parent
            parent_name = _tail.split(".")[-1] or _raw_parent
        try:
            _raw_type = str(_ad.get_tag_value("BlueprintType") or "")
        except:
            _raw_type = ""
        if _raw_type:
            bp_type = _raw_type.replace("BPTYPE_", "")
    except:
        pass

    result["basic_info"] = {{
        "name": bp.get_name(),
        "asset_path": bp_path,
        "parent_class": parent_name,
        "blueprint_type": bp_type,
    }}

    def _field(obj, *names):
        for _n in names:
            try:
                _v = getattr(obj, _n)
            except:
                continue
            if _v is not None:
                return _v
        return ""

    # 变量：C++ 反射 read_variables（UE5.1 Python 绑定无 new_variables）
    try:
        _raw_vars = bridge.read_variables(bp)
        for var in (_raw_vars or []):
            result["variables"].append({{
                "var_name": str(_field(var, "variable_name", "VariableName")),
                "var_type": str(_field(var, "variable_type", "VariableType")),
                "category": str(_field(var, "category", "Category") or "Default"),
                "default_value": str(_field(var, "default_value", "DefaultValue")),
                "is_exposed": bool(_field(var, "is_editable", "bIsEditable") or False),
                "is_array": bool(_field(var, "is_array", "bIsArray") or False),
            }})
    except:
        pass

    # 遍历所有 Graph（Bridge API）
    try:
        all_graphs = bridge.get_all_graphs(bp)
    except:
        all_graphs = []
        # fallback：逐个尝试
        for getter in [lambda: bridge.find_event_graph(bp), lambda: None]:
            try:
                g = getter()
                if g:
                    all_graphs.append(g)
            except:
                pass

    graph_count = 0
    node_count = 0

    for graph in (all_graphs or []):
        try:
            gname = graph.get_name()
        except:
            continue

        # 使用 Bridge ListNodes
        try:
            node_infos = bridge.list_nodes(graph)
        except:
            node_infos = []

        graph_nodes = []
        for ni in (node_infos or []):
            node_count += 1
            title = ni.node_title
            nclass = ni.node_class

            # 使用 Bridge GetPinInfos
            pins_list = []
            try:
                pin_infos = bridge.get_pin_infos(graph, ni)
                for pi in (pin_infos or []):
                    pins_list.append({{
                        "name": str(_field(pi, "pin_name", "PinName")),
                        "direction": str(_field(pi, "pin_direction", "PinDirection")),
                        "pin_type": str(_field(pi, "pin_type", "PinType")),
                        "is_connected": bool(_field(pi, "is_connected", "bIsConnected") or False),
                        "is_exec": bool(_field(pi, "is_execution_pin", "bIsExecutionPin") or False),
                        "default_value": str(_field(pi, "default_value", "DefaultValue")),
                    }})
            except:
                pass

            graph_nodes.append({{
                "title": title,
                "node_class": nclass,
                "node_guid": str(ni.node_guid) if hasattr(ni, 'node_guid') and ni.node_guid else "",
                "pins": pins_list,
                "is_event": nclass.startswith("K2Node_Event") if nclass else False,
            }})

        graph_count += 1
        result["graphs"][gname] = {{
            "graph_name": gname,
            "node_count": len(graph_nodes),
            "nodes": graph_nodes,
        }}

        # 连线拓扑（Bridge GetNodeConnections）
        try:
            connections = bridge.get_node_connections(graph)
            graph_conns = []
            for conn in (connections or []):
                _c = {{
                    "from_node": str(_field(conn, "from_node_title", "FromNodeTitle")),
                    "from_pin": str(_field(conn, "from_pin_name", "FromPinName")),
                    "from_guid": str(_field(conn, "from_node_guid", "FromNodeGuid")),
                    "to_node": str(_field(conn, "to_node_title", "ToNodeTitle")),
                    "to_pin": str(_field(conn, "to_pin_name", "ToPinName")),
                    "to_guid": str(_field(conn, "to_node_guid", "ToNodeGuid")),
                }}
                graph_conns.append(_c)
                result["connections"].append(_c)
            result["graphs"][gname]["connections"] = graph_conns
        except:
            pass

    result["basic_info"]["node_count_total"] = node_count
    result["basic_info"]["graph_count"] = graph_count
    result["ok"] = True

except Exception as e:
    result["errors"].append(f"L2 探针异常: {str(e)}")

print("__JSON_RESULT__" + json.dumps(result, ensure_ascii=False, default=str) + "__JSON_END__")
'''


# L3 探针：最低可用读取
PROBE_L3 = r'''
import json
import unreal

bp_path = {blueprint_path}

result = {{
    "ok": False,
    "level": "L3",
    "basic_info": {{}},
    "variables": [],
    "graphs": {{}},
    "connections": [],
    "errors": [],
}}

try:
    bp = unreal.EditorAssetLibrary.load_asset(bp_path)
    if bp:
        result["basic_info"] = {{
            "name": bp.get_name(),
            "asset_path": bp_path,
            "parent_class": getattr(bp, 'parent_class', lambda: None)().get_name() if hasattr(bp, 'parent_class') else "Unknown",
            "blueprint_type": bp.get_class().get_name(),
        }}

        # L3 只能确认蓝图可加载，无法提取节点细节
        result["graphs"]["_placeholder"] = {{
            "graph_name": "L3_MINIMAL",
            "node_count": 0,
            "nodes": [],
            "note": "L3 降级：无法读取节点详情，请检查 UE Python 环境和 Bridge 插件"
        }}
        result["ok"] = True
    else:
        result["errors"].append(f"加载失败: {bp_path}")

except Exception as e:
    result["errors"].append(f"L3 探针异常: {str(e)}")

print("__JSON_RESULT__" + json.dumps(result, ensure_ascii=False, default=str) + "__JSON_END__")
'''


# ═══════════════════════════════════════════════════════════
# BlueprintReader：主控制器
# ═══════════════════════════════════════════════════════════


class BlueprintReader:
    """蓝图逆向识别引擎。

    用法：
        reader = BlueprintReader()
        analysis = reader.analyze("/Game/Blueprints/BP_HelloMirror")
        print(analysis.to_json())

    运行模式：
        - remote：通过 ue_send.py 向 UE 发送探针代码（默认）
        - embedded：在 UE Python 控制台中直接运行
    """

    def __init__(self, mode: str = "remote"):
        self.mode = mode
        self.ue_send_path = _find_ue_send() if mode == "remote" else None
        self._last_raw_result: Optional[Dict] = None

    # ── 主入口 ──

    def analyze(self, blueprint_path: str) -> BlueprintAnalysis:
        """分析蓝图，返回完整 BlueprintAnalysis。

        Args:
            blueprint_path: UE 资产路径，如 "/Game/Blueprints/BP_HelloMirror"

        Returns:
            BlueprintAnalysis 结构化结果
        """
        t0 = time.time()
        analysis = BlueprintAnalysis(
            basic_info=BlueprintBasicInfo(
                name="",
                asset_path=blueprint_path,
                parent_class="Unknown",
            ),
            analysis_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        # 三级降级尝试
        # v2.20 修复（P2）：ue_send.py 缺失时 _exec_remote 抛 RuntimeError，
        #   旧降级循环不捕获 → 整个 analyze 崩溃、拿不到结构化结果。
        #   现在 RuntimeError 记入 errors 并继续三级循环（embedded 模式不受影响）。
        for level, probe_template in [
            (DegradeLevel.L1_NATIVE, PROBE_L1),
            (DegradeLevel.L2_BRIDGE, PROBE_L2),
            (DegradeLevel.L3_MINIMAL, PROBE_L3),
        ]:
            try:
                raw = self._run_probe(probe_template, blueprint_path)
            except RuntimeError as e:
                analysis.errors.append(f"{level.name} 探针执行失败: {e}")
                continue
            if raw is None:
                analysis.errors.append(f"{level.name} 探针：无响应")
                continue

            if raw.get("ok"):
                # 如果返回 ok 但 graphs 为空，且不是最后一级 → 继续降级
                graphs = raw.get("graphs", {})
                if not graphs and level != DegradeLevel.L3_MINIMAL:
                    analysis.warnings.append(f"{level.name} 探针返回空图数据，继续降级")
                    continue
                analysis.degrade_level = level
                self._parse_result(analysis, raw)
                break
            else:
                errs = raw.get("errors", [])
                analysis.errors.extend(errs)
                analysis.warnings.append(
                    f"{level.name} 探针失败: {'; '.join(errs[:2])}"
                )
                # L3 失败就真的没办法了
                if level == DegradeLevel.L3_MINIMAL:
                    analysis.errors.append("三级降级全部失败，无法读取蓝图")

        # 提取引用资产
        if analysis.degrade_level != DegradeLevel.L3_MINIMAL:
            self._extract_references(analysis)

        analysis.analysis_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._analysis_duration_ms = (time.time() - t0) * 1000

        return analysis

    # ── 探针执行 ──

    def _run_probe(self, template: str, bp_path: str) -> Optional[Dict]:
        """执行探针代码，返回解析后的 dict 或 None。"""
        code = template.replace('{blueprint_path}', repr(bp_path))
        code = code.replace('{{', '{').replace('}}', '}')

        if self.mode == "embedded":
            # 直接在 UE 环境中执行
            return self._exec_embedded(code)
        elif self.mode == "remote":
            return self._exec_remote(code)
        else:
            raise ValueError(f"未知模式: {self.mode}")

    def _exec_remote(self, code: str) -> Optional[Dict]:
        """通过 ue_send.py 远程执行探针代码。

        🔴 T-20260909-READFIX：结果改由临时文件回传。
        UE remote_execution 的 UDP 响应在含 pins/连线数据时会被截断
        （实测 BP_Player L2 探针 → "Unterminated string" → 误判"无响应"），
        故 stdout 仅作兜底，权威数据走文件。
        """
        if not self.ue_send_path:
            raise RuntimeError("ue_send.py 未找到，请设置 UE_SEND_PATH 环境变量")

        result_file = os.path.join(
            tempfile.gettempdir(),
            f"ue_probe_result_{os.getpid()}_{int(time.time() * 1000)}.json",
        )

        wrapper = r'''
import sys as _sys
import io as _io
_UE_RESULT_FILE = r"__RESULT_FILE__"
_UE_ORIG_STDOUT = _sys.stdout
_sys.stdout = _io.StringIO()
result = None
__USER_CODE__
_sys.stdout = _UE_ORIG_STDOUT
try:
    import json as _json
    with open(_UE_RESULT_FILE, "w", encoding="utf-8") as _f:
        _json.dump(result if result is not None else {"ok": False, "errors": ["probe produced no result"]}, _f, ensure_ascii=False, default=str)
except Exception:
    pass
'''
        wrapped_code = (wrapper
                        .replace("__RESULT_FILE__", result_file)
                        .replace("__USER_CODE__", code))

        # 写入临时文件
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", encoding="utf-8", delete=False
        ) as f:
            f.write(wrapped_code)
            tmp_path = f.name

        try:
            # 通过 ue_send.py 执行
            result = subprocess.run(
                [sys.executable, self.ue_send_path, f"exec(open(r'{tmp_path}', encoding='utf-8').read())"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=max(30, int(os.environ.get("PROBE_TIMEOUT", "60"))),  # B-005: 可配置超时
                cwd=os.path.dirname(self.ue_send_path),
            )
            output = result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return None
        except Exception as e:
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except:
                pass

        # 优先读结果文件（UDP 截断时 stdout 不可靠）；stdout 仅作兜底
        if os.path.isfile(result_file):
            try:
                with open(result_file, "r", encoding="utf-8") as rf:
                    data = json.load(rf)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
            finally:
                try:
                    os.unlink(result_file)
                except:
                    pass

        return self._parse_probe_output(output)

    def _exec_embedded(self, code: str) -> Optional[Dict]:
        """在 UE Python 环境中直接执行（embedded 模式）。

        🔒 A-006 安全审查：exec() 仅执行探针模板（静态常量，非用户输入），
        不构成注入风险。异常不再静默吞噬，记录到返回值中。

        B-012 修复：通过捕获 stdout 获取探针 JSON 输出，
        不再硬编码返回 {ok: False}。
        """
        import io
        local_ns = {}
        captured = io.StringIO()
        try:
            import sys
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                exec(code, {"__name__": "__main__"}, local_ns)
            finally:
                sys.stdout = old_stdout
        except SystemExit:
            pass
        except Exception as e:
            import traceback
            return {
                "ok": False,
                "errors": [f"{type(e).__name__}: {e}"],
                "traceback": traceback.format_exc(),
                "level": "embedded",
            }

        output = captured.getvalue()
        if output.strip():
            return self._parse_probe_output(output)
        return {"ok": False, "errors": ["embedded 探针无输出"], "level": "embedded"}

    def _parse_probe_output(self, output: str) -> Optional[Dict]:
        """从探针输出中提取 JSON 结果。"""
        marker_start = "__JSON_RESULT__"
        marker_end = "__JSON_END__"

        idx_start = output.find(marker_start)
        idx_end = output.find(marker_end)

        if idx_start == -1 or idx_end == -1:
            return None

        json_str = output[idx_start + len(marker_start):idx_end]
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return None

    # ── 结果解析 ──

    def _parse_result(self, analysis: BlueprintAnalysis, raw: Dict):
        """将探针返回的 dict 填充到 BlueprintAnalysis 中。"""
        # 基本信息
        bi = raw.get("basic_info", {})
        analysis.basic_info = BlueprintBasicInfo(
            name=bi.get("name", ""),
            asset_path=bi.get("asset_path", analysis.basic_info.asset_path),
            parent_class=bi.get("parent_class", "Unknown"),
            blueprint_type=bi.get("blueprint_type", ""),
            interfaces=bi.get("interfaces", []),
            node_count_total=bi.get("node_count_total", 0),
            graph_count=bi.get("graph_count", 0),
        )

        # 变量
        analysis.variables = [
            BlueprintVariable(
                var_name=v.get("var_name", ""),
                var_type=v.get("var_type", ""),
                default_value=v.get("default_value", ""),
                category=v.get("category", "Default"),
                is_exposed=v.get("is_exposed", True),
                is_array=v.get("is_array", False),
            )
            for v in raw.get("variables", [])
        ]

        # Graphs
        analysis.graphs = {}
        for gname, gdata in raw.get("graphs", {}).items():
            nodes = [
                NodeInfo(
                    title=n.get("title", ""),
                    node_class=n.get("node_class", ""),
                    node_guid=n.get("node_guid", ""),
                    is_event=n.get("is_event", False),
                    is_pure=n.get("is_pure", False),
                    pins=[
                        PinInfo(
                            name=p.get("name", ""),
                            direction=p.get("direction", ""),
                            pin_type=p.get("pin_type", ""),
                            is_connected=p.get("is_connected", False),
                            is_exec=p.get("is_exec", False),
                            default_value=p.get("default_value", ""),
                        )
                        for p in n.get("pins", [])
                    ],
                )
                for n in gdata.get("nodes", [])
            ]
            analysis.graphs[gname] = GraphAnalysis(
                graph_name=gname,
                node_count=len(nodes),
                nodes=nodes,
            )

        # 全局连线
        analysis.connections = [
            ConnectionInfo(
                from_node=c.get("from_node", ""),
                from_pin=c.get("from_pin", ""),
                from_guid=c.get("from_guid", ""),
                to_node=c.get("to_node", ""),
                to_pin=c.get("to_pin", ""),
                to_guid=c.get("to_guid", ""),
            )
            for c in raw.get("connections", [])
        ]

        # 错误和警告
        analysis.errors.extend(raw.get("errors", []))
        analysis.warnings.extend(raw.get("warnings", []))

    def _extract_references(self, analysis: BlueprintAnalysis):
        """从节点中提取引用资产（CastTo / CreateWidget / SpawnActor / LoadObject）。"""
        refs = []
        for gname, graph in analysis.graphs.items():
            for node in graph.nodes:
                # CastTo 节点
                if "K2Node_DynamicCast" in node.node_class or "K2Node_ClassDynamicCast" in node.node_class:
                    for pin in node.pins:
                        if "Cast" in pin.pin_type or pin.direction == "Output":
                            if pin.default_value:
                                refs.append(AssetReference(
                                    asset_path=pin.default_value,
                                    asset_type="CastTo",
                                    source_node=node.title,
                                    is_suspicious=pin.default_value.startswith("/Engine/"),
                                ))

                # CreateWidget
                if "CreateWidget" in node.title:
                    for pin in node.pins:
                        if "Class" in pin.name and pin.default_value:
                            refs.append(AssetReference(
                                asset_path=pin.default_value,
                                asset_type="CreateWidget",
                                source_node=node.title,
                            ))

                # SpawnActor
                if "SpawnActor" in node.title or "SpawnActorFromClass" in node.title:
                    for pin in node.pins:
                        if "Class" in pin.name and pin.default_value:
                            refs.append(AssetReference(
                                asset_path=pin.default_value,
                                asset_type="SpawnActor",
                                source_node=node.title,
                            ))

        # 去重（按 asset_path）
        seen = set()
        unique_refs = []
        for ref in refs:
            key = f"{ref.asset_path}|{ref.asset_type}"
            if key not in seen:
                seen.add(key)
                unique_refs.append(ref)
        analysis.referenced_assets = unique_refs

    # ── 便捷输出 ──

    def to_markdown_summary(self, analysis: BlueprintAnalysis) -> str:
        """生成 Markdown 摘要报告（步骤 1-3）。"""
        bi = analysis.basic_info

        def _cell(s, n=50):
            """Markdown 单元格清洗：UE 节点标题常含换行，会撑破表格。"""
            return str(s or "").replace("\n", " ").replace("\r", " ").replace("|", "/").strip()[:n]

        lines = [
            f"# 蓝图分析报告：{bi.name or '(未命名)'}",
            "",
            f"**分析时间：** {analysis.analysis_time}",
            f"**降级层级：** {analysis.degrade_level.name}",
            "",
            "## 1. 基本信息",
            "",
            f"| 属性 | 值 |",
            f"|:---|:---|",
            f"| 名称 | {bi.name} |",
            f"| 路径 | `{bi.asset_path}` |",
            f"| 父类 | {bi.parent_class} |",
            f"| 类型 | {bi.blueprint_type} |",
            f"| 接口 | {', '.join(bi.interfaces) if bi.interfaces else '(无)'} |",
            f"| 总节点数 | {bi.node_count_total} |",
            f"| Graph 数 | {bi.graph_count} |",
            "",
        ]

        # 变量
        lines.append("## 2. 变量清单")
        lines.append("")
        if analysis.variables:
            lines.append("| 名称 | 类型 | Category | 默认值 | 暴露 |")
            lines.append("|:---|:---|:---|:---|:---:|")
            for v in analysis.variables:
                lines.append(
                    f"| {v.var_name} | {v.var_type} | {v.category} | "
                    f"{v.default_value[:40] if v.default_value else '(空)'} | "
                    f"{'✅' if v.is_exposed else '❌'} |"
                )
        else:
            lines.append("*(无变量)*")
        lines.append("")

        # 节点摘要
        lines.append("## 3. 节点摘要")
        lines.append("")
        for gname, graph in analysis.graphs.items():
            lines.append(f"### {gname}（{graph.node_count} 节点）")
            lines.append("")
            if graph.nodes:
                lines.append("| 节点 | 类型 | 事件/纯 | 引脚数 |")
                lines.append("|:---|:---|:---:|:---:|")
                for node in graph.nodes[:50]:  # 最多 50 行
                    flags = []
                    if node.is_event:
                        flags.append("🔵事件")
                    if node.is_pure:
                        flags.append("🟢纯函数")
                    lines.append(
                        f"| {_cell(node.title, 50)} | {_cell(node.node_class, 40)} | "
                        f"{' '.join(flags) or '-'} | {len(node.pins)} |"
                    )
                if len(graph.nodes) > 50:
                    lines.append(f"| ... | *(省略 {len(graph.nodes) - 50} 个节点)* | | |")
            else:
                lines.append("*(无节点)*")
            lines.append("")

        # 连线摘要
        lines.append("## 连线拓扑")
        lines.append("")
        if analysis.connections:
            lines.append(f"共 {len(analysis.connections)} 条连线：")
            lines.append("")
            lines.append("| 源节点 | 源引脚 | → | 目标节点 | 目标引脚 |")
            lines.append("|:---|:---|:---:|:---|:---|")
            for conn in analysis.connections[:30]:
                lines.append(
                    f"| {_cell(conn.from_node, 30)} | {_cell(conn.from_pin, 20)} | → | "
                    f"{_cell(conn.to_node, 30)} | {_cell(conn.to_pin, 20)} |"
                )
            if len(analysis.connections) > 30:
                lines.append(f"| ... | *(省略 {len(analysis.connections) - 30} 条)* | | | |")
        else:
            lines.append("*(连线数据不可用——可能处于 L3 降级)*")
        lines.append("")

        # 引用资产
        if analysis.referenced_assets:
            lines.append("## 5. 引用资产")
            lines.append("")
            lines.append("| 资产路径 | 类型 | 来源节点 | 可疑 |")
            lines.append("|:---|:---|:---|:---:|")
            for ref in analysis.referenced_assets:
                lines.append(
                    f"| {_cell(ref.asset_path, 50)} | {_cell(ref.asset_type, 20)} | "
                    f"{_cell(ref.source_node, 30)} | {'⚠️' if ref.is_suspicious else ''} |"
                )
            lines.append("")

        # 错误/警告
        if analysis.errors:
            lines.append("## ❌ 错误")
            for e in analysis.errors:
                lines.append(f"- {e}")
            lines.append("")
        if analysis.warnings:
            lines.append("## ⚠️ 警告")
            for w in analysis.warnings:
                lines.append(f"- {w}")
            lines.append("")

        return "\n".join(lines)

    def save_report(self, analysis: BlueprintAnalysis,
                    output_path: Optional[str] = None) -> str:
        """保存分析报告到文件。

        Args:
            analysis: 分析结果
            output_path: 输出路径，默认 output/<蓝图名>_analysis.md

        Returns:
            实际保存路径
        """
        if output_path is None:
            name = analysis.basic_info.name or "unnamed"
            safe_name = name.replace("/", "_").replace("\\", "_")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = os.path.join(
                OUTPUT_DIR, f"{safe_name}_analysis_{timestamp}.md"
            )

        md = self.to_markdown_summary(analysis)
        # v2.20 修复（P2 两处崩溃/覆盖 bug）：
        #   ① `--output report.md`（无目录前缀）时 os.path.dirname 返回 ""，
        #      os.makedirs("") 抛 FileNotFoundError；
        #   ② json 路径用 replace(".md", ".json")：文件名/路径其它位置含 ".md"
        #      时会把 JSON 写到 Markdown 同一路径上，**覆盖刚写的报告** → splitext。
        out_dir = os.path.dirname(output_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(md)

        # 同时保存 JSON
        _base, _ext = os.path.splitext(output_path)
        json_path = _base + ".json"
        with open(json_path, "w", encoding="utf-8") as f:
            f.write(analysis.to_json())

        return output_path


# ═══════════════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════════════


def analyze_blueprint(bp_path: str,
                      output_path: Optional[str] = None,
                      mode: str = "remote") -> BlueprintAnalysis:
    """一键分析蓝图。

    用法：
        from blueprint_reader import analyze_blueprint
        analysis = analyze_blueprint("/Game/Blueprints/BP_HelloMirror")
        print(analysis.to_json())
    """
    reader = BlueprintReader(mode=mode)
    analysis = reader.analyze(bp_path)

    if output_path:
        reader.save_report(analysis, output_path)

    return analysis


def quick_inspect(bp_path: str, mode: str = "remote") -> str:
    """快速检查蓝图基本信息（返回 Markdown 摘要字符串）。"""
    reader = BlueprintReader(mode=mode)
    analysis = reader.analyze(bp_path)
    return reader.to_markdown_summary(analysis)


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    # Windows GBK 终端兼容：emoji / 中文输出不再崩溃（T-20260909-READFIX）
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="UE5 蓝图逆向识别引擎 — blueprint_reader.py"
    )
    parser.add_argument(
        "blueprint_path",
        nargs="?",
        help="蓝图资产路径，如 /Game/Blueprints/BP_HelloMirror"
    )
    parser.add_argument(
        "--output", "-o",
        help="输出文件路径（默认 output/<蓝图名>_analysis.md）"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出（而非 Markdown）"
    )
    parser.add_argument(
        "--mode",
        default="remote",
        choices=["remote", "embedded"],
        help="运行模式（默认 remote）"
    )
    parser.add_argument(
        "--ue-send",
        help="ue_send.py 路径（覆盖默认查找）"
    )

    args = parser.parse_args()

    if not args.blueprint_path:
        parser.print_help()
        sys.exit(1)

    reader = BlueprintReader(mode=args.mode)
    if args.ue_send:
        reader.ue_send_path = args.ue_send

    print(f"🔍 分析蓝图: {args.blueprint_path}")
    analysis = reader.analyze(args.blueprint_path)

    if args.json:
        print(analysis.to_json())
    else:
        md = reader.to_markdown_summary(analysis)
        print(md)

    if args.output:
        saved = reader.save_report(analysis, args.output)
        print(f"\n📄 报告已保存: {saved}")

    # 错误处理
    if analysis.errors:
        print(f"\n❌ {len(analysis.errors)} 个错误", file=sys.stderr)
        sys.exit(1)
