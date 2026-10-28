# C++ Bridge API 清单（59 UFUNCTION）

> 从 SKILL.md §2.3 迁出（v4.6 分层）。日常用 Python 命令层；本表用于确认
> 某个 API 在本机 DLL 是否存在（或直接查 `/diag` 的 `all_methods`）。

## C++ Bridge API 清单（v0.20 基线 59 项 · 后续版本新增见文末）

> **来源：** `<Project>\Plugins\BlueprintPythonBridge\Source\BlueprintPythonBridge\Public\BlueprintPythonBridge.h`（开发机工程路径已占位化）· SKILL-06 编译验证 + v0.8-v0.10 新增补齐（T-20260805 / T-20260822 / T-20260823）· **2026-09-12 复核**：随包 `.h`（21,619 B）实测 UFUNCTION = 59（历史基线；v4.5/v4.6 已新增 6 项）
> **口径：** 下表 1-21 为 SKILL-06 原始 21 API；22-27 为 v0.8-v0.10 新增（实测已入 DLL）。随包 DLL（901,120 B · 2026-09-18 编译）源码实测 UFUNCTION = **59** → （已由新节覆盖）；**28-59 项已于 2026-09-12 逐条补全**（T-20260912-RELEASE-V29-CLEAN），本表现覆盖随包 `.h` 全部 59 个 UFUNCTION，无遗漏项；能力判定仍以 `bridge_apis` health 实测 + 源码 `.h` 为准。
> **v3.7 增量（T-20261024-UE5BRIDGE-SCREENSHOT）：** `.h` 新增 3 个 UFUNCTION
> （CaptureViewport / CapturePIE / ProjectActorsToScreen → 合计 62）——**离线草稿
> 尚未编译**，随包 DLL 仍为 59；回家 `build_for_engine.bat` 编译后本节随包计数
> 再同步。旧 DLL 调用截图命令 → 明确的"重新编译"提示（fail-loud）。

#### 蓝图资产管理

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 1 | `CreateActorBlueprint` | `(AssetPath, AssetName) → UBlueprint*` | 创建 Actor 蓝图资产 |
| 2 | `CompileBlueprint` | `(UBlueprint*) → bool` | 编译蓝图 |
| 3 | `CompileAndSave` | `(UBlueprint*) → bool` | 编译并保存蓝图 |
| 4 | `AddMemberVariable` | `(Blueprint, VarName, VarType) → bool` | 添加成员变量 |

#### 图表管理

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 5 | `FindEventGraph` | `(UBlueprint*) → UEdGraph*` | 查找 EventGraph（纯查询） |
| 6 | `GetEventGraph` | `(UBlueprint*) → UEdGraph*` | 获取或创建 EventGraph |
| 7 | `CreateEventGraph` | `(UBlueprint*) → UEdGraph*` | 创建空 EventGraph |
| 8 | `GetAllGraphs` | `(UBlueprint*) → TArray<UEdGraph*>` | 获取所有图表 |

#### 节点添加

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 9 | `AddEventNode` | `(Graph, EventName, X, Y) → UEdGraphNode*` | 添加事件节点 |
| 10 | `AddFunctionCallNode` | `(Graph, FuncName, Class, X, Y) → UEdGraphNode*` | 添加函数调用节点 |
| 11 | `AddBranchNode` | `(Graph, X, Y) → UEdGraphNode*` | 添加 Branch 控制流节点 |
| 12 | `AddSequenceNode` | `(Graph, NumOutputs, X, Y) → UEdGraphNode*` | 添加 Sequence 控制流节点 |

#### 节点读取

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 13 | `ListNodes` | `(UEdGraph*) → TArray<FBPNodeInfo>` | 列出图中所有节点 |
| 14 | `ListPins` | `(UEdGraphNode*) → TArray<FBPPinInfo>` | 列出节点所有引脚 |
| 15 | `GetPinInfos` ⭐ | `(Graph, FBPNodeInfo&) → TArray<FBPPinInfo>` | 通过 FBPNodeInfo 远程查询引脚 |
| 16 | `FindNodeByTitle` | `(Graph, Title) → UEdGraphNode*` | 按标题精确查找节点 |

#### 连线操作

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 17 | `ConnectPins` | `(SrcNode, SrcPin, DstNode, DstPin) → bool` | 连接两个引脚 |
| 18 | `SetPinDefaultValue` | `(Node, PinName, Value) → bool` | 设置引脚默认值 |
| 19 | `GetNodeConnections` ⭐ | `(UEdGraph*) → TArray<FBPConnectionInfo>` | 读取全图连线拓扑 |
| 20 | `GetNodeConnectionsForNode` ⭐ | `(Graph, FBPNodeInfo&) → TArray<FBPConnectionInfo>` | 读取单节点所有连线 |

#### 图维护

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 21 | `ClearGraphNodes` | `(UEdGraph*) → void` | 清除图中所有节点 |

#### v0.8-v0.10 新增 API（清单同步补齐）

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 22 | `BreakPins` | `(SrcNode, SrcPin, DstNode, DstPin) → bool` | 断开两引脚间连线（v0.8 T-20260805：UE5.1 Python 无 EdGraphPin 绑定，只能 C++ 实现） |
| 23 | `RemoveNode` | `(Graph, Node) → bool` | 从图中移除节点（v0.8 T-20260805：Python 无 RemoveNode 绑定；Python delete_node 优先走此 API + remove_unused_nodes 降级） |
| 24 | `AddComponentToBlueprint` | `(Blueprint, CompName, CompClass, OutError&) → bool` | 给蓝图添加组件（v0.9 T-20260822：C++ SCS API，绕过 UE5.1 Python 无 SimpleConstructionScript 暴露限制。反射语义见下注①） |
| 25 | `SetComponentProperty` | `(Blueprint, CompName, PropName, PropValue, OutError&) → bool` | 设置组件属性（v0.10 T-20260823：SCS 节点模板 + FindFProperty + ImportText_Direct，替代 Python BPGC.ComponentTemplates 不可达路径。见下注①） |
| 26 | `ReadVariables` | `(UBlueprint*) → TArray<FBPVariableInfo>` | 读取蓝图变量清单（v0.10 T-20260823：C++ 反射 NewVariables + GeneratedClass 兜底，消除 Python new_variables 假阳性/降级。返回 FBPVariableInfo：VariableName/VariableType/DefaultValue/Category/bIsEditable） |
| 27 | `ExecutePythonOnGameThread` | `(Code, OutResult&) → void` | 在 GameThread 上执行任意 Python 代码（线程安全入口，供 /read 等非 GameThread 端点调用） |

> **注① · C++ bool + OutError 反射语义（裁决 A · 实测对齐）**：C++ 签名 `(bool, FString& OutError)` → UE Python 反射规则：`bool=false` → 返回 `None`（OutError 被吞）；`bool=true` → 返回 OutError 值（成功 `''` / 失败非空错误文本）→ **`''`=成功 / `None`=失败无文本 / 非空 str=失败+错误文本**。`add_component`（含 properties 逐属性 `set_component_property`）已按此语义解析。

#### 全量补齐：节点 / 事件 / 引脚 / 类级 API（28-59 · 2026-09-12 补全）

**节点通用操作**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 28 | `AddNodeByClass` | `(Graph, NodeClass, X, Y) → Node*` | 按 `TSubclassOf<UEdGraphNode>` 通用建节点（任意节点子类） |
| 29 | `SetNodePosition` | `(Node, X, Y) → bool` | 设置节点坐标（NodePosX/NodePosY + NotifyGraphChanged） |
| 30 | `GetNodeByGuid` | `(Graph, GuidStr) → Node*` | 按 NodeGuid 精确查找节点（解同名节点接错线的歧义） |
| 31 | `SetNodeProperty` | `(Node, PropName, Value) → bool` | 反射写节点属性（EnabledState / NodeComment / bCommentBubbleVisible / AdvancedPinDisplay 等） |

**变量与转换节点**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 32 | `AddVariableNode` | `(Graph, VarName, bIsSetter, X, Y) → Node*` | 变量 Get/Set 节点（走 SetFromProperty，不手拼 FMemberReference） |
| 33 | `AddCastNode` | `(Graph, TargetClassName, X, Y) → Node*` | 动态 Cast（类名支持 `/Script/Engine.Actor` 与 `/Game/.../BP_X.BP_X_C`） |
| 34 | `AddClassCastNode` | `(Graph, TargetClassName, X, Y) → Node*` | Class 动态 Cast（`UK2Node_ClassDynamicCast`，对类引用 Cast） |

**控制流 / 结构 / 表达式**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 35 | `AddMacroNode` | `(Graph, MacroName, X, Y) → Node*` | 标准宏实例（ForEachLoop / ForEachLoopWithBreak / WhileLoop / IsValid / Gate / DoOnce / MultiGate / Sequence / FlipFlop …） |
| 36 | `AddSwitchNode` | `(Graph, TypeKind, EnumOrTypeName, X, Y) → Node*` | Switch 节点（TypeKind ∈ `int`/`string`/`name`/`enum`；enum 时用 EnumOrTypeName 指定枚举） |
| 37 | `AddStructNode` | `(Graph, StructName, bIsBreak, X, Y) → Node*` | 结构体 Make/Break（Vector / Rotator / Transform / HitResult …） |
| 38 | `AddMathExpressionNode` | `(Graph, Expression, X, Y) → Node*` | 数学表达式节点（如 `(A+B)*C`；ReconstructNode 内触发 RebuildExpression） |

**输入事件**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 39 | `AddInputKeyEventNode` | `(Graph, KeyName, X, Y) → Node*` | 按键事件（W / A / SpaceBar / LeftMouseButton / RightMouseButton …） |
| 40 | `AddInputActionEventNode` | `(Graph, ActionName, X, Y) → Node*` | Legacy InputAction 事件（Enhanced Input 走本接口需实测，不可用降级为 39 按键事件） |
| 41 | `AddInputAxisEventNode` | `(Graph, AxisName, bIsKeyAxis, X, Y) → Node*` | 轴输入事件（`bIsKeyAxis=true` → InputAxisKeyEvent(FKey)；false → InputAxisEvent(FName)） |

**事件绑定**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 42 | `AddComponentBoundEventNode` | `(Graph, ComponentName, DelegateName, X, Y) → Node*` | 组件委托事件（`K2Node_ComponentBoundEvent`；电梯门 OnComponentBeginOverlap 走此接口） |
| 43 | `AddActorBoundEventNode` | `(Graph, ActorName, DelegateName, X, Y) → Node*` | 绑定关卡 Actor 实例事件（InitializeActorBoundEventParams；与 42 组件委托语义不同） |
| 44 | `AddCustomEventNode` | `(Graph, EventName, X, Y, SignatureFuncName) → Node*` | 自定义事件（先设 CustomFunctionName 再走建节点顺序；可选委托签名函数全路径/短名） |
| 45 | `AddCreateDelegateNode` | `(Graph, FunctionName, X, Y) → Node*` | CreateDelegate（Bind Event；SetFunction(FName)） |
| 46 | `AddEventDispatcher` | `(Blueprint, DelegateName, OutError&) → bool` | 建事件分发器（PC_MCDelegate 成员 + DelegateSignatureGraphs；引擎无公开静态 API，按 FBlueprintEditor 路径自建） |

**引脚 / 函数图**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 47 | `AddPinToNode` | `(Node, Direction, PinTypeStr, PinName, OutPinName&) → bool` | 任意节点动态加引脚（Direction = input\|output；PinTypeStr 语法见 cpp `BridgeParsePinType`） |
| 48 | `RemovePinFromNode` | `(Node, PinName) → bool` | 按引脚名移除节点引脚（UEdGraphNode::RemovePin） |
| 49 | `TryNodeAddInputPin` | `(Node) → bool` | 节点语义层加输入引脚（内部 Cast<IK2Node_AddPinInterface>，如 Switch / Select / FormatText） |
| 50 | `AddFunctionPinEntry` | `(Graph, PinName, PinTypeStr, bIsInput) → bool` | 函数图参数引脚（bIsInput=true → FunctionEntry 入参；false → FunctionResult 出参） |
| 51 | `AddLocalVariable` | `(Graph, VarName, VarType) → bool` | 函数图局部变量（写 UK2Node_FunctionEntry::LocalVariables + 重建 + 标记结构修改） |

**高级节点**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 52 | `AddCommentNode` | `(Graph, Text, X, Y, Width, Height) → Node*` | 注释框（`UEdGraphNode_Comment`；类属 UnrealEd 模块、Python 不可达） |
| 53 | `AddTimelineNode` | `(Graph, TimelineName, X, Y) → Node*` | Timeline 节点（先 AddNewTimeline 建模板，再建节点；空名自动取唯一名） |
| 54 | `AddCompositeNode` | `(Graph, X, Y) → Node*` | 折叠图 Composite（PostPlacedNewNode 内建 BoundGraph + Tunnel 进出节点） |
| 55 | `AddAsyncActionNode` | `(Graph, FactoryFunc, FactoryClass, ActivateFunc, X, Y) → Node*` | 异步任务节点（ProxyFactory* 为 protected UPROPERTY，走反射写入） |
| 56 | `AddEnumLiteralNode` | `(Graph, EnumName, ValueName, X, Y) → Node*` | 枚举字面量节点（EnumName 全路径/短名；ValueName 可选，写入 Enum 引脚默认值） |

**类级 / 组件**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 57 | `SetClassDefault` | `(Blueprint, PropName, Value) → bool` | 写类默认值（GeneratedClass CDO → FindFProperty + ImportText；解「PC 无实例」场景） |
| 58 | `RemoveComponent` | `(Blueprint, ComponentName, OutError&) → bool` | 移除 SCS 组件（语义对齐裁决 A：`''`=成功 / 非空=失败+错误文本） |
| 59 | `AddImplementedInterface` | `(Blueprint, InterfaceClassPath) → bool` | 蓝图实现新接口（FBlueprintEditorUtils::ImplementNewInterface） |

> ⭐ = SKILL-06 新增（3 个 API：`GetPinInfos`、`GetNodeConnections`、`GetNodeConnectionsForNode`）  
> 完整交付包：`output/BlueprintPythonBridge/`（DLL 901,120 B + PDB + .h + .uplugin）  
> 编译工具链：VS 2022 14.42 + UE 5.1 · `-NoParallelExecutor`（绕过 Win32Exception 998）

#### L2 Python 命令白名单（21 条 · 2026-09-12 在线实测 · 报告 §1 A 项）

> **来源（唯一权威）**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\L2-在线联调报告-20260912.md` §1「A 项 · 在线联调 21 项逐项表」（基线 = run_4 · `71 passed + 1 xfailed / 0 failed` · 72.42s）。
> **口径**：21 条 = `/command` 白名单的 **L2 批次**（白名单共 **72 条** = 15 基础 + 11 L1 + 21 L2 + 1 落盘 + 2 资产 + 9 v3.0能力 + 9 材质 + 4 UMG）；每条 = **≥1 正路径 + ≥1 fail-loud 负路径**（在线用例 `tests\test_pywrap_l2_online.py`，需 `--ue-online`）。**20 ✅ + 1 ⚠️(xfail) / 21**。
> **调用方式**：`POST http://127.0.0.1:8889/command` · 头 `X-Skill-Token: <token>` · 体 `{"command": "<命令名>", "params": {...}}`；未登记命令 → `Unknown command`（fail-loud，不执行任意代码）。
> **逐条详表**（完整签名 / 入参 / raw 锚点 / 风险注记）：`docs\L2-COMMANDS.md`；调度侧速查：`workflow.md` 附录 B。

| # | 命令 | 对应 C++ API | 关键入参 | 正路径（run_4 实测） | 负路径 fail-loud（run_4 实测） | 结论 |
|:--:|:---|:---|:---|:---|:---|:--:|
| 1 | `add_cast_node` | AddCastNode(#33) | bp_path, graph_name=EventGraph, target_class_name, node_pos | `title="类型转换为 Actor"` · 608ms | 不可解析全路径 → `拒绝调用引擎`；空值 → `参数为空`；图表不存在 → `图表未找到` | ✅ |
| 2 | `add_class_cast_node` | AddClassCastNode(#34) | bp_path, graph_name=EventGraph, target_class_name, node_pos | `title="类型转换为 Actor类"` · 594ms | 空值 → `参数为空` | ✅ |
| 3 | `add_macro_node` | AddMacroNode(#35) | bp_path, graph_name=EventGraph, macro_name, node_pos | `title="For Each Loop"` · 619ms；**24 候选宏名全扫 `ok=24 / fail=0`** | 未知宏名 → `返回 None（建节点失败）` | ✅ |
| 4 | `add_switch_node` | AddSwitchNode(#36) | bp_path, graph_name=EventGraph, type_kind, enum_or_type_name, node_pos | `int` → `切换整型`；`enum` → `切换ECollisionChannel` / `切换EInputEvent` | `type_kind 非法 'bogus'` | ✅ |
| 5 | `add_struct_node` | AddStructNode(#37) | bp_path, graph_name=EventGraph, struct_name, b_is_break, node_pos | `make` → `"创建Vector"`；`break` → `"中断Vector"` | 未知结构 → `返回 None`；空值 → `参数为空` | ✅ |
| 6 | `add_enum_literal_node` | AddEnumLiteralNode(#56) | bp_path, graph_name=EventGraph, enum_name, value_name, node_pos | `title="文字枚举ECollisionChannel"` ×2 | 空值 → `参数为空` | ✅ |
| 7 | `add_composite_node` | AddCompositeNode(#54) | bp_path, graph_name=EventGraph, node_pos | `title="EdGraph_0\n合并的图表"`（`pins` 回读为空 → 已留 warning，不静默） | 图表不存在 → `图表未找到` | ✅ |
| 8 | `add_math_expression_node` | AddMathExpressionNode(#38) | bp_path, graph_name=EventGraph, expression, node_pos | `title="(A + B) * C\n数学表达式"` · 653ms | 空表达式 → `参数为空`；图表不存在 | ✅ |
| 9 | `add_async_action_node` | AddAsyncActionNode(#55) | bp_path, graph_name=EventGraph, factory_function_name, factory_class_name, activate_function_name, node_pos | `title="延迟"` · 628ms（ProxyFactory 反射写入） | 不可解析类 → `拒绝调用引擎`；空类名 → `参数为空` | ✅ |
| 10 | `add_create_delegate_node` | AddCreateDelegateNode(#45) | bp_path, graph_name=EventGraph, function_name, node_pos | `title="创建事件"` · 590ms | 空 `function_name` → `参数为空` | ✅ |
| 11 | `add_actor_bound_event_node` | AddActorBoundEventNode(#43) | bp_path, graph_name=EventGraph, actor_name, delegate_name, node_pos | `title="Actor开始重叠时 (StaticMeshActor0)"` | 无该 Actor / 无该委托 → `返回 None`；空委托名 → `参数为空` | ✅ |
| 12 | `add_input_axis_event_node` | AddInputAxisEventNode(#41) | bp_path, graph_name=EventGraph, axis_name, b_is_key_axis, node_pos | 按键轴 `"W"`；命名轴 `"输入轴Move Forward / Backward"` | 非法键 → `返回 None`（**并自动清理残留**）；空 `axis_name` | ✅ |
| 13 | `add_input_action_event_node` | AddInputActionEventNode(#40) | bp_path, graph_name=EventGraph, action_name, node_pos | `title="输入操作Jump"` | 空 `action_name` → `参数为空` | ✅ |
| 14 | `add_component_bound_event_node` | AddComponentBoundEventNode(#42) | bp_path, graph_name=EventGraph, component_name, delegate_name, node_pos | `title="组件开始重叠时 (PywrapBox)"` | 无该组件 / 无该委托 → `返回 None` | ✅ |
| 15 | `add_pin_to_node` | AddPinToNode(#47) | bp_path, graph_name=EventGraph, node_guid, direction, pin_type, pin_name | `ok=true` | 同名引脚 → `引脚 'DupPin' 已存在…（禁静默双建）`；`direction 非法 'sideways'`；GUID 不命中 | ✅ |
| 16 | `remove_pin_from_node` | RemovePinFromNode(#48) | bp_path, graph_name=EventGraph, node_guid, pin_name | `ok=true · removed=true` | 无该引脚 → 报错并回读实读列表 | ✅ |
| 17 | `try_node_add_input_pin` | TryNodeAddInputPin(#49) | bp_path, graph_name=EventGraph, node_guid, strict | ⚠️ **XFAIL**（引擎层不支持：UE5.1 `K2Node_SwitchInteger` 未实现 `IK2Node_AddPinInterface`） | 同 unsupported 语义 fail-loud | ⚠️ xfail |
| 18 | `set_node_property` | SetNodeProperty(#31) | bp_path, graph_name=EventGraph, node_guid, property_name, value | `NodeComment` / `bCanRenameNode` 均 `ok=true` | GUID 不命中；未知属性 → `返回 False` | ✅ |
| 19 | `remove_component` | RemoveComponent(#58) | bp_path, comp_name | `ok=true`（`removed` 回读不可用 → `warnings[2]`，不静默） | 组件不存在 → `not found in SimpleConstructionScript` | ✅ |
| 20 | `add_implemented_interface` | AddImplementedInterface(#59) | bp_path, interface_class_path | 加固前实测 `ok=true` 330ms（D-1 族入口） | 不可解析全路径 → `拒绝调用引擎`；重复接口 → `返回 False`；🔴 **裸短名 = R2 硬崩 → R3 已硬化**（R5 真机复验：`BridgeError` 22ms · UE 未崩 · 合法裸短名不误拒） | ✅（含 R3 硬化） |
| 21 | `add_event_dispatcher` | AddEventDispatcher(#46) | bp_path, delegate_name | `ok=true` | 同名分发器 → `返回 None（错误文本被 UE 反射吞掉）`；空 `delegate_name` | ✅ |

> **小计**：**20 ✅ + 1 ⚠️(xfail) / 21**。⚠️ `try_node_add_input_pin` = 引擎层不支持（UE5.1 `K2Node_SwitchInteger` 未实现 `IK2Node_AddPinInterface`）→ 用例按 `xfail` 记录，调用方**不得依赖**该命令成功。
> 🔴 **风险注记（R3/R5 已硬化 · 已真机复验）**：`add_implemented_interface` 等 7 个「class/enum/struct/interface 路径入参」命令族，前置解析守卫已由「不可解析**全路径** → 拒绝调用引擎」扩展为「不可解析**裸短名** → 同样拒绝」（Tier B 硬化）。**引擎侧对不可解析路径会 `check()` 断言硬崩编辑器**（实测：`BP_PywrapL2Bare_20260912` 裸短名致 UnrealEditor 硬崩）→ 调用方必须传可加载路径（`/Script/Module.XXX` 或 `/Game/.../BPI_X.BPI_X_C`）。
> 📌 `read_nodes` 语义（R3）：`graph_name` 有值 → **只返回该图**；未指定/空串 → 全量（向后兼容）。




---

## 附：v4.5 / v4.6 新增（6 项）

| UFUNCTION | 版本 | 用途 |
|---|---|---|
| `BeginAgentTransaction(FString)` | v4.5 | 开启 agent 事务（嵌套只在最外层真正开） |
| `EndAgentTransaction()` | v4.5 | 结束 agent 事务（未开启时返回 false，不误关他人） |
| `CancelAgentTransaction()` | v4.5 | 丢弃当前 agent 事务记录（不产生撤销步） |
| `GetAgentUndoStatus()` | v4.5 | JSON：可撤销/可重做/agent 可回滚步数/下一步标题 |
| `UndoAgentTransactions(int32)` | v4.5 | 连续撤销 agent 步，遇用户步即停 |
| `CompileBlueprintStructured(UBlueprint*)` | v4.6 | 一次编译返回 FCompilerResultsLog 结构化错误（节点 GUID 权威） |
