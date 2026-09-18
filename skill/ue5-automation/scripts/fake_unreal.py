#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 🔴 KB 语义搜索结果（已贴注释顶部）：
#   - 跨图 Pin 操作必须校验同图归属（2026-07-24 ConnectPins C++ 经验）
#   - 端点返回结构必须与 blueprint_editor.py 调用方对齐（data.broken_links / method）
"""
fake_unreal.py — unreal 模块测试替身（UE5_BRIDGE_TEST_MODE=1 时使用）
=====================================================================
由 ue5_bridge.py 在 UE5_BRIDGE_TEST_MODE=1 时注入，模拟 UE5 编辑器 Python API
的极小可用面（仅覆盖 ue5_bridge.py 依赖的部分），用于离线集成测试。

这不是 Mock——这是一套真实可执行的内存蓝图模型：
  - FakeBlueprint / FakeGraph / FakeNode / FakePin / FakeConnection
  - BlueprintPythonBridge 静态方法（find_node_by_title / list_nodes / ...）
  - EditorAssetLibrary / SystemLibrary / Paths / BlueprintEditorLibrary
  - register_slate_pre_tick_callback → 后台线程模拟 GameThread tick

故障注入（测试 catalog_only 降级路径）：
  set_break_link_failure(True) → FakePin.break_link_to 抛异常

作者：dev（代码开发 Agent）
日期：2026-08-03
"""

import os
import sys
import time
import threading
import uuid

# ══════════════════════════════════════════════════════════
# 全局状态（fake 资产库 + 故障注入标志）
# ══════════════════════════════════════════════════════════

_FAKE_ASSETS = {}            # 资产路径 → FakeBlueprint
_FAKE_FAIL_BREAK = False     # 故障注入：break_link_to 抛异常
_ASSET_LOCK = threading.RLock()

# ══════════════════════════════════════════════════════════
# 数据模型
# ══════════════════════════════════════════════════════════

class FakePin:
    """模拟 UEdGraphPin。"""

    def __init__(self, name, direction="EGPD_Output", category="exec",
                 sub_category="", default_value=""):
        self.pin_name = name
        self.direction = direction
        self.pin_category = category
        self.pin_sub_category = sub_category
        self.default_value = default_value
        self.linked_to = []       # list[FakePin]（双向记录，模拟真实 linked_to）
        self._owner = None        # FakeNode

    @property
    def is_connected(self):
        return bool(self.linked_to)

    def get_owning_node(self):
        return self._owner

    def break_link_to(self, other):
        """模拟 UEdGraphPin.break_link_to(other_pin)。"""
        global _FAKE_FAIL_BREAK
        with _ASSET_LOCK:
            if _FAKE_FAIL_BREAK:
                raise RuntimeError("fake break_link_to failure (fault injection)")
            if other in self.linked_to:
                self.linked_to.remove(other)
            if self in other.linked_to:
                other.linked_to.remove(self)

    def make_link_to(self, other):
        with _ASSET_LOCK:
            if other not in self.linked_to:
                self.linked_to.append(other)
            if self not in other.linked_to:
                other.linked_to.append(self)


class FakeNode:
    """模拟 UEdGraphNode。"""

    def __init__(self, title, node_class="K2Node_Event", x=0, y=0):
        self.node_title = title
        self.node_class = node_class
        self.node_guid = str(uuid.uuid4())
        self.pos_x = x
        self.pos_y = y
        self.pins = []            # list[FakePin]
        self._destroyed = False
        self._graph = None
        self._props = {}   # L2：SetNodeProperty 写入值（回读用）

    def add_pin(self, name, direction="EGPD_Output", category="exec",
                default_value="", sub_category=""):
        p = FakePin(name, direction, category, sub_category, default_value)
        p._owner = self
        self.pins.append(p)
        return p

    def find_pin(self, name):
        for p in self.pins:
            if str(p.pin_name) == name:
                return p
        return None

    def get_editor_property(self, name):
        """L2：节点属性回读（对齐 UE Python get_editor_property）。"""
        key = _camel_to_snake(str(name))
        if key in self._props:
            return self._props[key]
        raise AttributeError(
            "fake node has no editor property %r" % (name,))

    def set_editor_property(self, name, value):
        """L2：节点属性写入（对齐 UE Python set_editor_property）。"""
        self._props[_camel_to_snake(str(name))] = value

    def destroy_node(self):
        self._destroyed = True

    def get_name(self):
        return self.node_title


class FakeGraph:
    """模拟 UEdGraph。

    T-20260912-UE5BRIDGE-TITLEGATE-FIX：新增 `_all` **全量节点表**，支持
    「同 title 异 GUID」场景（旧实现 `_nodes` 标题字典会互相覆盖 → 同名节点在桩里
    直接丢失，无法离线复现歧义）。语义对齐真实 UE：
      · `_nodes[title]`（→ find_node_by_title）只返回**其中一个**同名节点；
      · `get_nodes()` / `list_nodes()` 返回**全部**节点（含同名）；
      · `get_node_by_guid()` 遍历全量 → 同名场景下按 GUID 精确命中。
    """

    def __init__(self, name="EventGraph"):
        self.name = name
        self._nodes = {}          # title → FakeNode（同名时保留最后写入者，模拟"只返回一个"）
        self._all = []            # 全量节点（含同名，插入序）

    def get_name(self):
        return self.name

    def add_node(self, node):
        node._graph = self
        self._nodes[node.node_title] = node
        self._all.append(node)
        return node

    def remove_node(self, node):
        title = getattr(node, "node_title", None)
        if node not in self._all:
            raise KeyError(f"node not found: {title}")
        self._all.remove(node)
        if title and self._nodes.get(title) is node:
            del self._nodes[title]
            # 同名节点仍存在 → 回填一个（保持 find_node_by_title 可用）
            for n in self._all:
                if getattr(n, "node_title", None) == title:
                    self._nodes[title] = n
                    break
        else:
            for k, v in list(self._nodes.items()):
                if v is node:
                    del self._nodes[k]
                    break
        return

    def get_nodes(self):
        return list(self._all)


class FakeBlueprint:
    """模拟 unreal.Blueprint 资产。"""

    def __init__(self, path):
        self.path = path
        self.graphs = {}          # graph_name → FakeGraph
        self.dirty = False
        self.new_variables = []
        self.blueprint_type = "Blueprint"
        self.status = "Unknown"
        self._generated_class = None
        # T-20260822-ADDCOMPONENT-CPP：组件模板列表（模拟 C++ SCS 添加后
        # 注册到 BPGC.ComponentTemplates 的行为）
        self.components = []      # list[FakeActorComponent]
        self._interfaces = []     # L2：已实现接口短名

    def get_name(self):
        base = self.path.rstrip("/").split("/")[-1]
        return base.split(".")[-1]

    def get_path_name(self):
        return self.path

    def get_event_graph(self):
        if "EventGraph" in self.graphs:
            return self.graphs["EventGraph"]
        return next(iter(self.graphs.values()), None)

    def add_graph(self, graph):
        self.graphs[graph.name] = graph
        graph._bp = self   # L2：图 → 属主蓝图（组件事件节点校验用）
        return graph

    def mark_package_dirty(self):
        self.dirty = True

    def generated_class(self):
        if self._generated_class is None:
            self._generated_class = FakeGeneratedClass(self)
        return self._generated_class


class FakeGeneratedClass:
    def __init__(self, bp):
        self._bp = bp
        self._cdo = None

    def get_super_class(self):
        return FakeClass("Actor")

    def get_default_object(self):
        # T-20260910-UE5BRIDGE-PYWRAP-L1-IMPL：CDO 需稳定（SetClassDefault
        # 写 + 回读读同一对象），故缓存而非每次新建。
        if self._cdo is None:
            self._cdo = FakeCDO(self._bp)
        return self._cdo

    @property
    def implemented_interfaces(self):
        # L2：返回真实已实现接口（T-20260912-PYWRAP-L2-IMPL）
        return list(getattr(self._bp, "_interfaces", []))

    # T-20260822-ADDCOMPONENT-CPP：对齐 UBlueprintGeneratedClass::ComponentTemplates
    # 的 UE Python 反射名 component_templates
    @property
    def component_templates(self):
        return list(getattr(self._bp, "components", []))


class FakeActorComponent:
    """模拟 UActorComponent（C++ SCS 添加的组件模板）。"""

    # T-20260823-UE5BRIDGE-CPP-APIS 子项1：已知属性白名单（模拟真实组件 UPROPERTY
    # 集合；C++ 侧 FindFProperty 找不到属性 → 非空错误文本）。属性名用 C++ 原名
    # （BoxExtent / SphereRadius），与真实 C++ API SetComponentProperty 一致。
    KNOWN_PROPERTIES = {
        "SceneComponent": {"Mobility", "RelativeLocation", "RelativeRotation",
                           "RelativeScale3D", "ComponentTags"},
        "StaticMeshComponent": {"Mobility", "StaticMesh", "ComponentTags"},
        "BoxComponent": {"Mobility", "BoxExtent", "ComponentTags"},
        "SphereComponent": {"Mobility", "SphereRadius", "ComponentTags"},
        "CapsuleComponent": {"Mobility", "CapsuleHalfHeight", "CapsuleRadius",
                             "ComponentTags"},
        "ArrowComponent": {"ArrowSize", "ComponentTags"},
        "PointLightComponent": {"Intensity", "LightColor", "AttenuationRadius",
                                "ComponentTags"},
        "SpotLightComponent": {"Intensity", "AttenuationRadius", "InnerConeAngle",
                               "OuterConeAngle", "ComponentTags"},
        "AudioComponent": {"Sound", "ComponentTags"},
        "ParticleSystemComponent": {"Template", "ComponentTags"},
        "TextRenderComponent": {"Text", "TextRenderColor", "ComponentTags"},
    }

    def __init__(self, name, component_class):
        self._name = name
        self.component_class = component_class
        self._props = {}

    def get_name(self):
        return self._name

    def set_editor_property(self, name, value):
        self._props[name] = value

    def get_editor_property(self, name):
        return self._props.get(name)

    # 对齐 UE Python 反射：UPROPERTY 直接 setattr 也可用（测试用）
    def __getattr__(self, item):
        return self._props.get(item)


class FakeClass:
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


class FakeCDO:
    def __init__(self, bp):
        self._bp = bp
        # T-20260910-UE5BRIDGE-PYWRAP-L1-IMPL：CDO 属性存储（SetClassDefault
        # 写入 + 封装层回读校验）
        self._props = {}

    def set_editor_property(self, name, value):
        self._props[name] = value

    def get_editor_property(self, name):
        return self._props.get(name)

    def __getattr__(self, item):
        return None


class FakeLocalVariable:
    """模拟 FBPVariableDescription（函数图局部变量）。"""

    def __init__(self, var_name, var_type):
        self.var_name = var_name
        self.var_type = var_type


class FakeConnection:
    """模拟 Bridge get_node_connections 返回的连接结构。"""

    def __init__(self, from_node_title, from_pin_name, to_node_title, to_pin_name):
        self.from_node_title = from_node_title
        self.from_pin_name = from_pin_name
        self.to_node_title = to_node_title
        self.to_pin_name = to_pin_name


# ══════════════════════════════════════════════════════════
# BlueprintPythonBridge（模拟 C++ 插件 21 API 的子集）
# ══════════════════════════════════════════════════════════

class BlueprintPythonBridge:
    """模拟 BlueprintPythonBridge C++ 插件（ue5_bridge.py 依赖的子集）。"""

    @staticmethod
    def get_version():
        return "9.9.9"

    @staticmethod
    def load_blueprint_asset(path):
        with _ASSET_LOCK:
            return _FAKE_ASSETS.get(path)

    @staticmethod
    def get_all_graphs(bp):
        with _ASSET_LOCK:
            return list(bp.graphs.values())

    @staticmethod
    def get_event_graph(bp):
        return bp.get_event_graph()

    @staticmethod
    def find_node_by_title(graph, title):
        if graph is None:
            return None
        with _ASSET_LOCK:
            return graph._nodes.get(title)

    @staticmethod
    def list_nodes(graph):
        if graph is None:
            return []
        with _ASSET_LOCK:
            # TITLEGATE-FIX：返回全量节点（含同名）→ 支持同名计数/歧义判定
            return graph.get_nodes()

    @staticmethod
    def list_pins(node):
        return list(node.pins)

    @staticmethod
    def get_pin_infos(graph, node):
        return list(node.pins)

    @staticmethod
    def get_node_connections(graph):
        with _ASSET_LOCK:
            conns = []
            seen = set()
            for node in graph.get_nodes():
                for pin in node.pins:
                    for other in pin.linked_to:
                        key = tuple(sorted((id(pin), id(other))))
                        if key in seen:
                            continue
                        seen.add(key)
                        conns.append(FakeConnection(
                            node.node_title, str(pin.pin_name),
                            other._owner.node_title if other._owner else "?",
                            str(other.pin_name),
                        ))
            return conns

    @staticmethod
    def get_node_connections_for_node(graph, node):
        with _ASSET_LOCK:
            conns = []
            seen = set()
            for pin in node.pins:
                for other in pin.linked_to:
                    key = tuple(sorted((id(pin), id(other))))
                    if key in seen:
                        continue
                    seen.add(key)
                    conns.append(FakeConnection(
                        node.node_title, str(pin.pin_name),
                        other._owner.node_title if other._owner else "?",
                        str(other.pin_name),
                    ))
            return conns

    @staticmethod
    def connect_pins(src_node, src_pin_name, dst_node, dst_pin_name):
        with _ASSET_LOCK:
            src_pin = src_node.find_pin(src_pin_name) if src_node else None
            dst_pin = dst_node.find_pin(dst_pin_name) if dst_node else None
            if not src_pin or not dst_pin:
                return False
            src_pin.make_link_to(dst_pin)
            return True

    @staticmethod
    def compile_blueprint(bp):
        return True

    @staticmethod
    def add_member_variable(bp, var_name, var_type):
        bp.new_variables.append({"var_name": var_name, "var_type": var_type})
        return True

    @staticmethod
    def add_component_to_blueprint(bp, comp_name, comp_class):
        """模拟 C++ AddComponentToBlueprint(UBlueprint*, FString, FString, FString&).

        T-20260822-ADDCOMPONENT-ONLINE 阶段1实测 + T-20260822-
        ADDCOMPONENT-RENAME-ERR 引擎源码实锤：UE Python 反射规则
        （PyGenUtil.cpp PackReturnValues）——bool=false → Python None
        （OutError 被吞）；bool=true → 返回 OutError 值。故语义：
          成功 = ''（空字符串）/ 失败（bool=false 类）= None /
          失败（重名，C++ return true + OutError 非空）= 非空错误文本。
        组件模板注册到 bp.components（模拟 SCS 编译后填充 BPGC.ComponentTemplates）。
        """
        with _ASSET_LOCK:
            if bp is None:
                return None
            if not comp_name:
                return "ComponentName is empty"
            for tpl in getattr(bp, "components", []):
                if tpl.get_name() == comp_name:
                    return f"Component '{comp_name}' already exists"
            comp = FakeActorComponent(comp_name, comp_class)
            bp.components.append(comp)
            return ""

    @staticmethod
    def set_component_property(bp, comp_name, prop_name, prop_value):
        """模拟 C++ SetComponentProperty(UBlueprint*, FString, FString, FString, FString&).

        T-20260823-UE5BRIDGE-CPP-APIS 子项1：对齐裁决 A 语义——
          成功 = ''（空字符串）/ 失败（bool=false 类）= None /
          失败（组件不存在 / 属性不存在，C++ return true + OutError 非空）=
          非空错误文本。
        属性存在性按组件类 KNOWN_PROPERTIES 白名单判定（模拟 FindFProperty）。
        """
        with _ASSET_LOCK:
            if bp is None:
                return None
            if not comp_name or not prop_name:
                return "ComponentName and PropertyName must be non-empty"
            for tpl in getattr(bp, "components", []):
                if tpl.get_name() == comp_name:
                    known = FakeActorComponent.KNOWN_PROPERTIES.get(
                        tpl.component_class, set())
                    if prop_name not in known:
                        return (f"Property '{prop_name}' not found on component "
                                f"'{comp_name}' (class {tpl.component_class})")
                    tpl.set_editor_property(prop_name, prop_value)
                    return ""
            return f"Component '{comp_name}' not found in SimpleConstructionScript"

    @staticmethod
    def read_variables(bp):
        """模拟 C++ ReadVariables(UBlueprint*) → TArray<FBPVariableInfo>。

        T-20260823-UE5BRIDGE-CPP-APIS 子项2：返回变量清单（含类型），
        结构对齐 FBPVariableInfo USTRUCT（UE Python 反射 snake_case：
        variable_name / variable_type / default_value / category / is_editable）。
        fake 的 new_variables 条目是 {"var_name","var_type"} dict，
        统一转成 FBPVariableInfo 结构 dict。
        """
        with _ASSET_LOCK:
            result = []
            for var in getattr(bp, "new_variables", []) or []:
                if isinstance(var, dict):
                    result.append({
                        "variable_name": str(var.get("var_name", "")),
                        "variable_type": str(var.get("var_type", "")),
                        "default_value": str(var.get("default_value", "")),
                        "category": str(var.get("category", "")),
                        "is_editable": bool(var.get("is_editable", False)),
                    })
                else:
                    result.append({
                        "variable_name": str(getattr(var, "var_name", "")),
                        "variable_type": str(getattr(var, "var_type", "")),
                        "default_value": str(getattr(var, "default_value", "")),
                        "category": str(getattr(var, "category", "")),
                        "is_editable": bool(getattr(var, "is_editable", False)),
                    })
            return result

    @staticmethod
    def create_actor_blueprint(pkg, name):
        path = pkg.rstrip("/") + "/" + name
        bp = FakeBlueprint(path)
        bp.add_graph(FakeGraph("EventGraph"))
        with _ASSET_LOCK:
            _FAKE_ASSETS[path] = bp
        return bp

    @staticmethod
    def set_pin_default_value(node, pin_name, value):
        """对齐真实 C++ DLL 签名：SetPinDefaultValue(Node, PinName, Value) 3 参。

        T-20260805-UE-FULL-CLOSURE：fake 原为 4 参（graph,node_id,pin_name,value），
        与真实 DLL 3 参不一致 → 修改为 3 参，保证集成测试与真实 UE 行为一致。
        """
        pin = node.find_pin(pin_name) if node else None
        if not pin:
            return False
        pin.default_value = value
        return True

    @staticmethod
    def remove_node(graph, node):
        """模拟 C++ RemoveNode(UEdGraph*, UEdGraphNode*)。

        T-20260806-FIX（派单 T-20260806-UE5AUTOMATION-FIX-AND-VERIFY Fix 1）：
          此前 fake 缺 remove_node → ue5_bridge._delete_node_on_game_thread Step 2
          hasattr(BPB,'remove_node')=False → 走 else 降级分支 → 目标节点残留
          （跨图）连线 → 报「节点仍有 1 条连线」→ /delete_node 被拒。
          补齐后与真实 C++ RemoveNode 行为对齐（支持已连接节点删除），
          同时清理该节点所有 pin 的双向连线引用（对齐真实 UE RemoveNode 语义）。
        """
        with _ASSET_LOCK:
            try:
                for pin in list(getattr(node, "pins", None) or []):
                    for other in list(getattr(pin, "linked_to", None) or []):
                        ol = getattr(other, "linked_to", None)
                        if ol is not None and pin in ol:
                            ol.remove(pin)
                    pin.linked_to = []
                graph.remove_node(node)
                return True
            except Exception:
                return False

    @staticmethod
    def execute_python_on_game_thread(code):
        # 测试模式不使用 /read 的临时文件桥接路径
        return None

    # ════════════════════════════════════════════════════════
    #  PATCH-A/B 新接口离线替身（T-20260910-UE5BRIDGE-PYWRAP-L1-IMPL · 11 API）
    #  语义与真实 C++ DLL 对齐：指针返回 → 失败 None；bool 返回 → 失败 False
    # ════════════════════════════════════════════════════════

    #: 建节点类名 → 默认引脚（简化，够离线断言 GUID / 引脚即可）
    NODE_CLASS_PINS = {
        "K2Node_IfThenElse": [("execute", "EGPD_Input", "exec"),
                              ("Condition", "EGPD_Input", "bool"),
                              ("then", "EGPD_Output", "exec"),
                              ("else", "EGPD_Output", "exec")],
        "K2Node_CallFunction": [("execute", "EGPD_Input", "exec"),
                                ("then", "EGPD_Output", "exec")],
    }

    #: Timeline 节点标准 10 引脚（对齐 T-20260910 S4 在线实测）
    TIMELINE_PINS = ["Play", "PlayFromStart", "Stop", "Reverse",
                     "ReverseFromEnd", "Update", "Finished", "SetNewTime",
                     "NewTime", "Direction"]

    # 故障注入开关（离线复现真实缺陷场景）
    DROP_VARIABLE_PINS = False   # 变量不存在 → 静默空节点（E1 类）
    TIMELINE_PIN_LIMIT = None    # 限制 timeline 引脚数（验证 min_count 断言）
    DROP_COMMENT_TEXT = False    # 复现 D-2（注释文本不写入）

    @staticmethod
    def _ensure_class_node(graph, cls_name):
        """取（或建）指定类的节点（函数图入口/出口，替身内部使用）。"""
        for n in graph.get_nodes():
            if getattr(n, "node_class", "") == cls_name:
                return n
        node = FakeNode(cls_name, cls_name, 0, 0)
        graph.add_node(node)
        return node

    @staticmethod
    def add_node_by_class(graph, node_class, pos_x=0, pos_y=0):
        """AddNodeByClass（L1-1）：graph/class 非法 → None。"""
        if graph is None or node_class is None:
            return None
        getter = getattr(node_class, "get_name", None)
        cls_name = str(getter()) if callable(getter) else str(node_class)
        if not cls_name or cls_name == "None":
            return None
        node = FakeNode(cls_name, cls_name, int(pos_x), int(pos_y))
        for pname, direction, cat in BlueprintPythonBridge.NODE_CLASS_PINS.get(
                cls_name, [("execute", "EGPD_Input", "exec"),
                           ("then", "EGPD_Output", "exec")]):
            node.add_pin(pname, direction, cat)
        with _ASSET_LOCK:
            graph.add_node(node)
        return node

    @staticmethod
    def set_node_position(node, pos_x, pos_y):
        """SetNodePosition（L1-2）：node 非法 → False。"""
        if node is None:
            return False
        node.pos_x = int(pos_x)
        node.pos_y = int(pos_y)
        return True

    @staticmethod
    def get_node_by_guid(graph, guid):
        """GetNodeByGuid（L1-3）：未命中 → None。"""
        if graph is None:
            return None
        target = _norm_guid(guid)
        if not target:
            return None
        with _ASSET_LOCK:
            for n in graph.get_nodes():
                if _norm_guid(n.node_guid) == target:
                    return n
        return None

    @staticmethod
    def add_variable_node(graph, var_name, b_is_setter, pos_x=0, pos_y=0):
        """AddVariableNode（L1-4）：graph/var_name 非法 → None。

        `DROP_VARIABLE_PINS=True` 复现「变量不存在 → 返回节点但引脚缺失」
        （E1 类静默空节点），用于验证封装的引脚回读硬校验。
        """
        if graph is None or not var_name:
            return None
        is_setter = bool(b_is_setter)
        node = FakeNode(str(var_name),
                        "K2Node_VariableSet" if is_setter
                        else "K2Node_VariableGet",
                        int(pos_x), int(pos_y))
        with _ASSET_LOCK:
            graph.add_node(node)
            if not BlueprintPythonBridge.DROP_VARIABLE_PINS:
                node.add_pin(str(var_name), "EGPD_Input", "var",
                             default_value="0.0")
                node.add_pin("Output_Get", "EGPD_Output", "var")
            node.add_pin("self", "EGPD_Input", "object")
        return node

    @staticmethod
    def add_custom_event_node(graph, event_name, pos_x=0, pos_y=0,
                              signature_func=""):
        """AddCustomEventNode（L1-5）：graph/event_name 非法 → None。

        标题形如 `<name>\\n自定义事件`（对齐 T-20260910 D-2 修复后在线实测）。
        """
        if graph is None or not event_name:
            return None
        node = FakeNode(f"{event_name}\n自定义事件", "K2Node_CustomEvent",
                        int(pos_x), int(pos_y))
        node.add_pin("OutputDelegate", "EGPD_Output", "delegate")
        node.add_pin("then", "EGPD_Output", "exec")
        with _ASSET_LOCK:
            graph.add_node(node)
        return node

    @staticmethod
    def add_input_key_event_node(graph, key_name, pos_x=0, pos_y=0):
        """AddInputKeyEventNode（L1-6）：graph/key_name 非法 → None。"""
        if graph is None or not key_name:
            return None
        node = FakeNode(f"按键 {key_name}", "K2Node_InputKey",
                        int(pos_x), int(pos_y))
        node.add_pin("Pressed", "EGPD_Output", "exec")
        node.add_pin("Released", "EGPD_Output", "exec")
        node.add_pin("Key", "EGPD_Output", "struct")
        with _ASSET_LOCK:
            graph.add_node(node)
        return node

    @staticmethod
    def add_function_pin_entry(graph, pin_name, pin_type, b_is_input):
        """AddFunctionPinEntry（L1-7a）：graph/pin_name 非法 → False。

        `b_is_input=True` → FunctionEntry 入参；`False` → FunctionResult 出参。
        """
        if graph is None or not pin_name:
            return False
        cls_name = ("K2Node_FunctionEntry" if b_is_input
                    else "K2Node_FunctionResult")
        with _ASSET_LOCK:
            target = BlueprintPythonBridge._ensure_class_node(graph, cls_name)
            if target.find_pin(pin_name) is None:
                target.add_pin(str(pin_name),
                               "EGPD_Input" if b_is_input else "EGPD_Output",
                               "var", sub_category=str(pin_type))
        return True

    @staticmethod
    def add_local_variable(graph, var_name, var_type):
        """AddLocalVariable（L1-7b）：graph/var_name 非法 → False。"""
        if graph is None or not var_name:
            return False
        with _ASSET_LOCK:
            entry = BlueprintPythonBridge._ensure_class_node(
                graph, "K2Node_FunctionEntry")
            lv = getattr(entry, "local_variables", None)
            if lv is None:
                lv = []
                entry.local_variables = lv
            for v in lv:
                if getattr(v, "var_name", "") == var_name:
                    return True
            lv.append(FakeLocalVariable(var_name, var_type))
        return True

    @staticmethod
    def add_timeline_node(graph, timeline_name, pos_x=0, pos_y=0):
        """AddTimelineNode（L1-8）：graph/timeline_name 非法 → None。"""
        if graph is None or not timeline_name:
            return None
        node = FakeNode(str(timeline_name), "K2Node_Timeline",
                        int(pos_x), int(pos_y))
        pins = BlueprintPythonBridge.TIMELINE_PINS
        limit = BlueprintPythonBridge.TIMELINE_PIN_LIMIT
        for pname in (pins[:limit] if limit else pins):
            node.add_pin(pname, "EGPD_Output", "exec")
        with _ASSET_LOCK:
            graph.add_node(node)
        return node

    @staticmethod
    def add_comment_node(graph, text, pos_x=0, pos_y=0, width=400, height=200):
        """AddCommentNode（L1-9）：graph 非法 → None。

        `DROP_COMMENT_TEXT=True` 复现 D-2（文本不写入 → 标题为默认值），
        用于验证封装的「标题回读校验」fail-loud。
        """
        if graph is None:
            return None
        title = "Comment" if BlueprintPythonBridge.DROP_COMMENT_TEXT else str(text)
        node = FakeNode(title, "EdGraphNode_Comment", int(pos_x), int(pos_y))
        node.node_width = int(width)
        node.node_height = int(height)
        with _ASSET_LOCK:
            graph.add_node(node)
        return node

    @staticmethod
    def set_class_default(bp, property_name, value):
        """SetClassDefault（L1-10）：bp/property_name 非法 → False。"""
        if bp is None or not property_name:
            return False
        cdo = bp.generated_class().get_default_object()
        cdo.set_editor_property(_camel_to_snake(property_name), value)
        return True


# ══════════════════════════════════════════════════════════
    # ════════════════════════════════════════════════════════
    #  PATCH-A/B 剩余 21 项离线替身（T-20260912-UE5BRIDGE-PYWRAP-L2-IMPL）
    #  语义对齐真实 C++ DLL：
    #    · 指针返回（UEdGraphNode*）→ 失败 None
    #    · bool 返回（AddPinToNode 的 OutPinName 例外）→ 失败 False / None
    #    · 「bool + FString& OutError」→ '' 成功 / None 失败 / 非空 = 失败+文本
    # ════════════════════════════════════════════════════════

    # 故障注入开关（set_l2_* 模块级函数改写）
    L2_FAIL_PIN_ADD = False            # add_pin_to_node → None
    L2_FAIL_NODE_CTOR = None           # 命令名 → 建节点返回 None
    L2_TRY_ADD_PIN_NOOP = False        # TryNodeAddInputPin: True 但不加引脚
    L2_FAIL_NODE_PROP = False          # SetNodeProperty → False
    L2_DROP_NODE_PROP = False          # SetNodeProperty: True 但不写入
    L2_COMPONENT_REMOVE_RESULT = None  # 覆写 RemoveComponent 返回
    L2_DROP_CTOR_NODE = False          # 建节点返回对象但不入图（回读降级）

    @staticmethod
    def _l2_ctor_failed(graph, key):
        """建节点类命令的统一失败闸门（graph 空 / 指定注入 → True）。"""
        if graph is None:
            return True
        return BlueprintPythonBridge.L2_FAIL_NODE_CTOR == key

    @staticmethod
    def _l2_new_node(graph, title, node_class, pos_x, pos_y, pins=None):
        node = FakeNode(str(title), node_class, int(pos_x), int(pos_y))
        for pname, direction, cat in (pins or [("execute", "EGPD_Input", "exec"),
                                               ("then", "EGPD_Output", "exec")]):
            node.add_pin(pname, direction, cat)
        if not BlueprintPythonBridge.L2_DROP_CTOR_NODE:
            with _ASSET_LOCK:
                graph.add_node(node)
        return node

    @staticmethod
    def _l2_bool_result(ok):
        """「bool + FString& OutError」替身：成功 → ''；失败 → None。"""
        return "" if ok else None

    # — L2-01 · AddPinToNode（bool + FString& OutPinName）—

    @staticmethod
    def add_pin_to_node(node, direction, pin_type, pin_name):
        """AddPinToNode（cpp:2876）→ 成功返回实际引脚名 / 失败 None。

        重名 → 失败（对齐 C++ "pin already exists" → return false）。
        """
        if BlueprintPythonBridge.L2_FAIL_PIN_ADD:
            return None
        if node is None or not direction or not pin_name:
            return None
        if node.find_pin(str(pin_name)) is not None:
            return None
        d = ("EGPD_Input" if str(direction).strip().lower() == "input"
             else "EGPD_Output")
        with _ASSET_LOCK:
            node.add_pin(str(pin_name), d, str(pin_type) or "exec")
        return str(pin_name)

    # — L2-02 · RemovePinFromNode（bool）—

    @staticmethod
    def remove_pin_from_node(node, pin_name):
        if node is None or not pin_name:
            return False
        pin = node.find_pin(str(pin_name))
        if pin is None:
            return False
        with _ASSET_LOCK:
            node.pins.remove(pin)
        return True

    # — L2-03 · TryNodeAddInputPin（bool）—

    @staticmethod
    def try_node_add_input_pin(node):
        if node is None:
            return False
        supported = ("K2Node_Switch", "K2Node_Select", "K2Node_FormatText")
        cls = str(getattr(node, "node_class", ""))
        if not any(cls.startswith(s) for s in supported):
            return False
        if BlueprintPythonBridge.L2_TRY_ADD_PIN_NOOP:
            return True
        with _ASSET_LOCK:
            node.add_pin(f"Option_{len(node.pins)}", "EGPD_Input", "exec")
        return True

    # — L2-04 · SetNodeProperty（bool）—

    @staticmethod
    def set_node_property(node, property_name, value):
        if BlueprintPythonBridge.L2_FAIL_NODE_PROP:
            return False
        if node is None or not property_name:
            return False
        if BlueprintPythonBridge.L2_DROP_NODE_PROP:
            # 返回 True 但写入哨兵值（≠ 请求值）→ 验证回读不一致 fail-loud
            node.set_editor_property(str(property_name), "__DROPPED__")
            return True
        node.set_editor_property(str(property_name), str(value))
        return True

    # — L2-05 · AddCastNode —

    @staticmethod
    def add_cast_node(graph, target_class_name, pos_x=0, pos_y=0):
        if BlueprintPythonBridge._l2_ctor_failed(graph, "add_cast_node") \
                or not target_class_name:
            return None
        return BlueprintPythonBridge._l2_new_node(
            graph, f"Cast To {_short_name(target_class_name)}",
            "K2Node_DynamicCast", pos_x, pos_y,
            pins=[("execute", "EGPD_Input", "exec"),
                  ("Object", "EGPD_Input", "object"),
                  ("then", "EGPD_Output", "exec"),
                  ("AsObject", "EGPD_Output", "object")])

    # — L2-06 · AddClassCastNode —

    @staticmethod
    def add_class_cast_node(graph, target_class_name, pos_x=0, pos_y=0):
        if BlueprintPythonBridge._l2_ctor_failed(graph, "add_class_cast_node") \
                or not target_class_name:
            return None
        return BlueprintPythonBridge._l2_new_node(
            graph, f"Cast To {_short_name(target_class_name)} Class",
            "K2Node_ClassDynamicCast", pos_x, pos_y,
            pins=[("execute", "EGPD_Input", "exec"),
                  ("Class", "EGPD_Input", "class"),
                  ("then", "EGPD_Output", "exec"),
                  ("AsClass", "EGPD_Output", "class")])

    # — L2-07 · AddMacroNode —

    @staticmethod
    def add_macro_node(graph, macro_name, pos_x=0, pos_y=0):
        if BlueprintPythonBridge._l2_ctor_failed(graph, "add_macro_node") \
                or not macro_name:
            return None
        known = {"ForEachLoop", "ForEachLoopWithBreak", "WhileLoop", "IsValid",
                 "Gate", "FlipFlop", "DoOnce", "MultiGate", "Sequence"}
        if str(macro_name) not in known:
            return None
        return BlueprintPythonBridge._l2_new_node(
            graph, str(macro_name), "K2Node_MacroInstance", pos_x, pos_y,
            pins=[("execute", "EGPD_Input", "exec"),
                  ("Array", "EGPD_Input", "array"),
                  ("LoopBody", "EGPD_Output", "exec"),
                  ("Completed", "EGPD_Output", "exec")])

    # — L2-08 · AddSwitchNode —

    @staticmethod
    def add_switch_node(graph, type_kind, enum_or_type_name, pos_x=0, pos_y=0):
        kind = str(type_kind or "").strip().lower()
        if BlueprintPythonBridge._l2_ctor_failed(graph, "add_switch_node"):
            return None
        if kind not in ("int", "string", "name", "enum"):
            return None
        if kind == "enum" and not str(enum_or_type_name or "").strip():
            return None
        cls = "K2Node_Switch" + kind.capitalize()
        title = ("Switch on " + _short_name(enum_or_type_name)) if kind == "enum" \
            else "Switch on " + kind.capitalize()
        return BlueprintPythonBridge._l2_new_node(
            graph, title, cls, pos_x, pos_y,
            pins=[("execute", "EGPD_Input", "exec"),
                  ("Selection", "EGPD_Input", kind),
                  ("Default", "EGPD_Output", "exec")])

    # — L2-09 · AddStructNode —

    @staticmethod
    def add_struct_node(graph, struct_name, b_is_break, pos_x=0, pos_y=0):
        if BlueprintPythonBridge._l2_ctor_failed(graph, "add_struct_node") \
                or not struct_name:
            return None
        brk = bool(b_is_break)
        cls = "K2Node_BreakStruct" if brk else "K2Node_MakeStruct"
        title = ("Break " if brk else "Make ") + _short_name(struct_name)
        return BlueprintPythonBridge._l2_new_node(
            graph, title, cls, pos_x, pos_y,
            pins=[("execute", "EGPD_Input", "exec"),
                  ("StructRef", "EGPD_Input", "struct"),
                  ("then", "EGPD_Output", "exec")])

    # — L2-10 · AddEnumLiteralNode —

    @staticmethod
    def add_enum_literal_node(graph, enum_name, value_name, pos_x=0, pos_y=0):
        if BlueprintPythonBridge._l2_ctor_failed(graph, "add_enum_literal_node") \
                or not enum_name:
            return None
        node = BlueprintPythonBridge._l2_new_node(
            graph, _short_name(enum_name), "K2Node_EnumLiteral", pos_x, pos_y,
            pins=[("Enum", "EGPD_Output", "enum")])
        if str(value_name or ""):
            node.set_editor_property("EnumValue", str(value_name))
        return node

    # — L2-11 · AddCompositeNode —

    @staticmethod
    def add_composite_node(graph, pos_x=0, pos_y=0):
        if BlueprintPythonBridge._l2_ctor_failed(graph, "add_composite_node"):
            return None
        return BlueprintPythonBridge._l2_new_node(
            graph, "Composite", "K2Node_Composite", pos_x, pos_y,
            pins=[("execute", "EGPD_Input", "exec"),
                  ("then", "EGPD_Output", "exec")])

    # — L2-12 · AddMathExpressionNode —

    @staticmethod
    def add_math_expression_node(graph, expression, pos_x=0, pos_y=0):
        if BlueprintPythonBridge._l2_ctor_failed(graph,
                                                "add_math_expression_node") \
                or not expression:
            return None
        return BlueprintPythonBridge._l2_new_node(
            graph, f'"{expression}"', "K2Node_MathExpression", pos_x, pos_y,
            pins=[("A", "EGPD_Input", "float"),
                  ("B", "EGPD_Input", "float"),
                  ("", "EGPD_Output", "float")])

    # — L2-13 · AddAsyncActionNode —

    @staticmethod
    def add_async_action_node(graph, factory_function_name,
                              factory_class_name, activate_function_name,
                              pos_x=0, pos_y=0):
        if BlueprintPythonBridge._l2_ctor_failed(graph,
                                                 "add_async_action_node"):
            return None
        if not factory_function_name or not factory_class_name:
            return None
        return BlueprintPythonBridge._l2_new_node(
            graph, f"Async {factory_function_name}",
            "K2Node_AsyncAction", pos_x, pos_y,
            pins=[("execute", "EGPD_Input", "exec"),
                  ("then", "EGPD_Output", "exec"),
                  ("OnCompleted", "EGPD_Output", "delegate")])

    # — L2-14 · AddCreateDelegateNode —

    @staticmethod
    def add_create_delegate_node(graph, function_name, pos_x=0, pos_y=0):
        if BlueprintPythonBridge._l2_ctor_failed(graph,
                                                 "add_create_delegate_node") \
                or not function_name:
            return None
        return BlueprintPythonBridge._l2_new_node(
            graph, f"Create Event ({function_name})", "K2Node_CreateDelegate",
            pos_x, pos_y,
            pins=[("execute", "EGPD_Input", "exec"),
                  ("OutputDelegate", "EGPD_Output", "delegate")])

    # — L2-15 · AddActorBoundEventNode —

    @staticmethod
    def add_actor_bound_event_node(graph, actor_name, delegate_name,
                                   pos_x=0, pos_y=0):
        if BlueprintPythonBridge._l2_ctor_failed(graph,
                                                 "add_actor_bound_event_node"):
            return None
        if not actor_name or not delegate_name:
            return None
        return BlueprintPythonBridge._l2_new_node(
            graph, f"{actor_name} {delegate_name}", "K2Node_ActorBoundEvent",
            pos_x, pos_y,
            pins=[("OutputDelegate", "EGPD_Output", "delegate"),
                  ("then", "EGPD_Output", "exec")])

    # — L2-16 · AddInputAxisEventNode —

    @staticmethod
    def add_input_axis_event_node(graph, axis_name, b_is_key_axis,
                                  pos_x=0, pos_y=0):
        if BlueprintPythonBridge._l2_ctor_failed(graph,
                                                 "add_input_axis_event_node"):
            return None
        if not axis_name:
            return None
        if bool(b_is_key_axis):
            cls, title = "K2Node_InputAxisKeyEvent", f"InputAxisKey {axis_name}"
        else:
            cls, title = "K2Node_InputAxisEvent", f"InputAxis {axis_name}"
        return BlueprintPythonBridge._l2_new_node(
            graph, title, cls, pos_x, pos_y,
            pins=[("AxisValue", "EGPD_Output", "float"),
                  ("then", "EGPD_Output", "exec")])

    # — L2-17 · AddInputActionEventNode —

    @staticmethod
    def add_input_action_event_node(graph, action_name, pos_x=0, pos_y=0):
        if BlueprintPythonBridge._l2_ctor_failed(
                graph, "add_input_action_event_node") or not action_name:
            return None
        return BlueprintPythonBridge._l2_new_node(
            graph, f"InputAction {action_name}", "K2Node_InputAction",
            pos_x, pos_y,
            pins=[("Pressed", "EGPD_Output", "exec"),
                  ("Released", "EGPD_Output", "exec")])

    # — L2-18 · AddComponentBoundEventNode（SCS 组件委托）—

    @staticmethod
    def add_component_bound_event_node(graph, component_name, delegate_name,
                                       pos_x=0, pos_y=0):
        if BlueprintPythonBridge._l2_ctor_failed(
                graph, "add_component_bound_event_node"):
            return None
        if not component_name or not delegate_name:
            return None
        bp = getattr(graph, "_bp", None)
        comps = [t.get_name() for t in getattr(bp, "components", [])] \
            if bp is not None else []
        if str(component_name) not in comps:
            return None
        return BlueprintPythonBridge._l2_new_node(
            graph, f"{component_name} {delegate_name}",
            "K2Node_ComponentBoundEvent", pos_x, pos_y,
            pins=[("OutputDelegate", "EGPD_Output", "delegate"),
                  ("then", "EGPD_Output", "exec")])

    # — L2-19 · RemoveComponent（bool + FString& OutError）—

    @staticmethod
    def remove_component(bp, comp_name):
        if BlueprintPythonBridge.L2_COMPONENT_REMOVE_RESULT is not None:
            return BlueprintPythonBridge.L2_COMPONENT_REMOVE_RESULT
        if bp is None:
            return None
        if not comp_name:
            return "ComponentName is empty"
        for tpl in list(getattr(bp, "components", [])):
            if tpl.get_name() == comp_name:
                with _ASSET_LOCK:
                    bp.components.remove(tpl)
                return ""
        return (f"Component '{comp_name}' not found in "
                f"SimpleConstructionScript")

    # — L2-20 · AddImplementedInterface（bool）—

    @staticmethod
    def add_implemented_interface(bp, interface_class_path):
        if bp is None or not interface_class_path:
            return False
        ifaces = getattr(bp, "_interfaces", None)
        if ifaces is None:
            ifaces = []
            bp._interfaces = ifaces
        short = _short_name(interface_class_path)
        if short in ifaces:
            return False
        ifaces.append(short)
        return True

    # — L2-21 · AddEventDispatcher（bool + FString& OutError；失败全走 false）—

    @staticmethod
    def add_event_dispatcher(bp, delegate_name):
        if bp is None or not delegate_name:
            return None
        graphs = getattr(bp, "delegate_signature_graphs", None)
        if graphs is None:
            graphs = []
            bp.delegate_signature_graphs = graphs
        if any(getattr(g, "name", "") == delegate_name for g in graphs):
            return None
        graphs.append(FakeGraph(delegate_name))
        return ""

# unreal 模块级 API
# ══════════════════════════════════════════════════════════

class EditorAssetLibrary:
    @staticmethod
    def load_asset(path):
        with _ASSET_LOCK:
            if path in _FAKE_ASSETS:
                return _FAKE_ASSETS[path]
            norm = "/Game/" + path.lstrip("/")
            return _FAKE_ASSETS.get(norm)

    @staticmethod
    def does_asset_exist(path):
        with _ASSET_LOCK:
            if path in _FAKE_ASSETS:
                return True
            return ("/Game/" + path.lstrip("/")) in _FAKE_ASSETS

    @staticmethod
    def delete_asset(path):
        with _ASSET_LOCK:
            _FAKE_ASSETS.pop(path, None)
            _FAKE_ASSETS.pop("/Game/" + path.lstrip("/"), None)

    @staticmethod
    def list_assets(path="/Game/", recursive=True):
        with _ASSET_LOCK:
            return [p for p in _FAKE_ASSETS if p.startswith(path)]


class SystemLibrary:
    @staticmethod
    def get_engine_version():
        return "5.1.1-fake"

    @staticmethod
    def get_game_name():
        return "FakeGame"


class Paths:
    @staticmethod
    def project_dir():
        return "C:/Fake/Project/"

    @staticmethod
    def project_log_dir():
        return "C:/Fake/Project/Saved/Logs/"


class BlueprintEditorLibrary:
    @staticmethod
    def compile_blueprint(bp):
        return True

    @staticmethod
    def remove_graph_node(bp, node):
        for g in bp.graphs.values():
            try:
                g.remove_node(node)
                return True
            except KeyError:
                continue
        return False

    @staticmethod
    def add_member_variable(bp, var_name, var_type):
        BlueprintPythonBridge.add_member_variable(bp, var_name, var_type)

    @staticmethod
    def get_blueprint_simple_construction_script(bp):
        return None

    @staticmethod
    def add_component_to_blueprint(bp, comp_class, comp_name):
        return None

    @staticmethod
    def add_implemented_interface(bp, iface_class):
        return None


def load_class(outer, path):
    return FakeClass(path.split(".")[-1])


def get_default_object(obj):
    """unreal.get_default_object(类) → CDO（T-20260910-PYWRAP-L1 新增）。"""
    getter = getattr(obj, "get_default_object", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return None
    return None


def _norm_guid(guid):
    """GUID 归一化（去 {} 去 - 大写），供新接口替身比对。"""
    s = str(guid or "")
    for ch in ("{", "}", "-"):
        s = s.replace(ch, "")
    return s.strip().upper()


def _camel_to_snake(name):
    """CamelCase → snake_case（对齐 UE Python 反射的 UPROPERTY 命名）。"""
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and not name[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


class BlueprintFactory:
    def __init__(self):
        self._props = {}

    def set_editor_property(self, name, value):
        self._props[name] = value


class AssetToolsHelpers:
    @staticmethod
    def get_asset_tools():
        return FakeAssetTools()


class FakeAssetTools:
    def create_asset(self, name, pkg, cls, factory):
        path = pkg.rstrip("/") + "/" + name
        bp = FakeBlueprint(path)
        bp.add_graph(FakeGraph("EventGraph"))
        with _ASSET_LOCK:
            _FAKE_ASSETS[path] = bp
        return bp


# ══════════════════════════════════════════════════════════
# GameThread tick 模拟（后台线程周期调用 slate tick callback）
# ══════════════════════════════════════════════════════════

_fake_tick_callbacks = []
_fake_tick_stop = threading.Event()
_fake_tick_thread = None


def _fake_tick_worker():
    while not _fake_tick_stop.is_set():
        for cb in list(_fake_tick_callbacks):
            try:
                cb(0.0)
            except Exception:
                pass
        time.sleep(0.005)


def register_slate_pre_tick_callback(cb):
    global _fake_tick_thread
    _fake_tick_callbacks.append(cb)
    if _fake_tick_thread is None or not _fake_tick_thread.is_alive():
        _fake_tick_stop.clear()
        _fake_tick_thread = threading.Thread(target=_fake_tick_worker, daemon=True)
        _fake_tick_thread.start()
    return "fake_slate_tick_handle"


def unregister_slate_pre_tick_callback(handle):
    _fake_tick_callbacks.clear()


# ══════════════════════════════════════════════════════════
# 测试辅助
# ══════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════
# L2 离线替身 · 模块级辅助（T-20260912-UE5BRIDGE-PYWRAP-L2-IMPL）
# ══════════════════════════════════════════════════════════

def _short_name(path):
    """类/枚举路径 → 短名（`/Script/Engine.Actor` → `Actor`；`BP_X.BP_X_C` → `BP_X`）。"""
    s = str(path or "").rstrip("/")
    last = s.split("/")[-1]
    if "." in last:
        last = last.split(".")[-1]
    if last.endswith("_C"):
        last = last[:-2]
    return last


def set_l2_fail_pin_add(fail=True):
    """故障注入：add_pin_to_node 返回 None（模拟 DLL 层失败）。"""
    BlueprintPythonBridge.L2_FAIL_PIN_ADD = bool(fail)


def set_l2_fail_node_ctor(cmd_name=None):
    """故障注入：指定命令的建节点调用返回 None（C++ nullptr）。"""
    BlueprintPythonBridge.L2_FAIL_NODE_CTOR = cmd_name or None


def set_l2_try_add_pin_noop(fail=True):
    """故障注入：TryNodeAddInputPin 返回 True 但不真加引脚（验证 strict 断言）。"""
    BlueprintPythonBridge.L2_TRY_ADD_PIN_NOOP = bool(fail)


def set_l2_fail_node_prop(fail=True):
    """故障注入：SetNodeProperty 返回 False。"""
    BlueprintPythonBridge.L2_FAIL_NODE_PROP = bool(fail)


def set_l2_drop_node_prop(fail=True):
    """故障注入：SetNodeProperty 返回 True 但不写入（验证回读 fail-loud）。"""
    BlueprintPythonBridge.L2_DROP_NODE_PROP = bool(fail)


def set_l2_component_remove_result(value=None):
    """故障注入：覆写 RemoveComponent 返回值（None=真实语义）。"""
    BlueprintPythonBridge.L2_COMPONENT_REMOVE_RESULT = value


def set_l2_drop_ctor_node(fail=True):
    """故障注入：建节点成功但**不留快照可读节点**（验证 GUID 回读告警降级）。"""
    BlueprintPythonBridge.L2_DROP_CTOR_NODE = bool(fail)


def _reset_l2_faults():
    """恢复全部 L2 故障注入开关（供 reset_fake 调用）。"""
    BlueprintPythonBridge.L2_FAIL_PIN_ADD = False
    BlueprintPythonBridge.L2_FAIL_NODE_CTOR = None
    BlueprintPythonBridge.L2_TRY_ADD_PIN_NOOP = False
    BlueprintPythonBridge.L2_FAIL_NODE_PROP = False
    BlueprintPythonBridge.L2_DROP_NODE_PROP = False
    BlueprintPythonBridge.L2_COMPONENT_REMOVE_RESULT = None
    BlueprintPythonBridge.L2_DROP_CTOR_NODE = False


def reset_fake():
    """清空 fake 资产库 + 恢复故障注入开关。"""
    global _FAKE_FAIL_BREAK
    with _ASSET_LOCK:
        _FAKE_ASSETS.clear()
        _FAKE_FAIL_BREAK = False
    set_variable_pins_drop(False)
    set_timeline_pin_limit(None)
    set_comment_text_drop(False)
    _reset_l2_faults()   # L2：恢复 21 项替身故障开关


def add_test_node(graph, title, node_class="K2Node_CallFunction", x=0, y=0, pins=None):
    """测试辅助：向图中追加节点（**允许同 title 异 GUID**，复现同名歧义场景）。

    T-20260912-UE5BRIDGE-TITLEGATE-FIX 新增。
    pins: [(name, direction, category), ...]；不传则给一对 exec 入/出引脚。
    """
    node = FakeNode(title, node_class, x, y)
    for spec in (pins if pins is not None else
                 [("then", "EGPD_Output", "exec"),
                  ("execute", "EGPD_Input", "exec")]):
        node.add_pin(spec[0], spec[1], spec[2])
    with _ASSET_LOCK:
        graph.add_node(node)
    return node


def create_test_blueprint(path="/Game/Test/BP_TestActor"):
    """创建测试蓝图（含默认 EventGraph）。"""
    bp = FakeBlueprint(path)
    bp.add_graph(FakeGraph("EventGraph"))
    with _ASSET_LOCK:
        _FAKE_ASSETS[path] = bp
    return bp


def connect_pins_by_name(graph, a_title, a_pin, b_title, b_pin):
    """在同一个图内连接两个节点引脚（模拟 Bridge connect_pins）。"""
    a = graph._nodes[a_title]
    b = graph._nodes[b_title]
    return BlueprintPythonBridge.connect_pins(a, a_pin, b, b_pin)


def connect_across_graphs(graph_a, a_title, a_pin, graph_b, b_title, b_pin):
    """跨图连接（模拟非法跨图连线，用于跨图同属校验测试）。"""
    a = graph_a._nodes[a_title]
    b = graph_b._nodes[b_title]
    return BlueprintPythonBridge.connect_pins(a, a_pin, b, b_pin)


def set_break_link_failure(fail=True):
    """故障注入：让 FakePin.break_link_to 抛异常（触发 catalog_only 降级）。"""
    global _FAKE_FAIL_BREAK
    _FAKE_FAIL_BREAK = bool(fail)


def set_variable_pins_drop(fail=True):
    """故障注入：变量节点不带变量引脚（复现 E1 类静默空节点）。"""
    BlueprintPythonBridge.DROP_VARIABLE_PINS = bool(fail)


def set_timeline_pin_limit(n):
    """故障注入：限制 timeline 引脚数（验证 min_count 硬断言）。"""
    BlueprintPythonBridge.TIMELINE_PIN_LIMIT = n


def set_comment_text_drop(fail=True):
    """故障注入：注释文本不写入节点（复现 D-2）。"""
    BlueprintPythonBridge.DROP_COMMENT_TEXT = bool(fail)


def get_asset(path):
    with _ASSET_LOCK:
        return _FAKE_ASSETS.get(path)


# 模块导入时确保初始状态干净
reset_fake()
