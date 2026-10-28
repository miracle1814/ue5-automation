# L2 命令参考（`/command` 白名单 · L2 批次 21 条 · 在线实测 2026-09-12）

> **口径来源**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\L2-在线联调报告-20260912.md` §1「A 项 21 项逐项表」·基线 = 在线 run_4（`71 passed + 1 xfailed / 0 failed` · 72.42s）。
> **归属**：T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE-R4（纯离线文档单 · 遗留项 L3/§20 #5）。
> **总数**：白名单 **72 条** = 15 基础 + 11 L1 + 21 L2 + 1 落盘 + 2 资产（2026-09-17 `ast` 实测订正，原记 48 条）（本文只覆盖 **L2 21 条**；全量速查见 `workflow.md` 附录 B）。

## 0 · 调用契约（所有 L2 命令通用）

| 项 | 内容 |
|:---|:---|
| 端点 | `POST http://127.0.0.1:8889/command`（Bridge 跑在 **UnrealEditor 进程内**） |
| 认证 | 头 `X-Skill-Token: <token>`（token 文件 `%APPDATA%\ue5_bridge\token`）；缺失/错误 → `403 Unauthorized` |
| 请求体 | `{"command": "<命令名>", "params": {...}, "timeout": 60.0}` |
| 响应 | `{"success": bool, "result": {...}, "error": str, "duration_ms": int}` |
| 失败语义 | **fail-loud**：`success=false` + `error="BridgeError: …"`（禁静默/禁假成功） |
| 未登记命令 | `Unknown command: <x>. Available: [...]`（白名单外一律拒绝） |
| 通用入参 | `bp_path`（必须 `/Game/...` 全路径）· `graph_name`（默认 `EventGraph`）· `node_pos`（`[x, y]` 或 `null`） |

## 1 · 21 条逐条参考

### 1. `add_cast_node`

- **入参**：`(bp_path, graph_name=EventGraph, target_class_name, node_pos)`
- **对应 C++ API**：AddCastNode(#33)
- **正路径（在线实测 run_4）**：`title="类型转换为 Actor"` · 608ms
- **负路径 fail-loud（在线实测 run_4）**：不可解析全路径 → `拒绝调用引擎`；空值 → `参数为空`；图表不存在 → `图表未找到`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b1_add_cast_node_pos.json` 等

### 2. `add_class_cast_node`

- **入参**：`(bp_path, graph_name=EventGraph, target_class_name, node_pos)`
- **对应 C++ API**：AddClassCastNode(#34)
- **正路径（在线实测 run_4）**：`title="类型转换为 Actor类"` · 594ms
- **负路径 fail-loud（在线实测 run_4）**：空值 → `参数为空`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b1_add_class_cast_node_pos.json` 等

### 3. `add_macro_node`

- **入参**：`(bp_path, graph_name=EventGraph, macro_name, node_pos)`
- **对应 C++ API**：AddMacroNode(#35)
- **正路径（在线实测 run_4）**：`title="For Each Loop"` · 619ms；**24 候选宏名全扫 `ok=24 / fail=0`**
- **负路径 fail-loud（在线实测 run_4）**：未知宏名 → `返回 None（建节点失败）`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b1_add_macro_node_pos.json` 等

### 4. `add_switch_node`

- **入参**：`(bp_path, graph_name=EventGraph, type_kind, enum_or_type_name, node_pos)`
- **对应 C++ API**：AddSwitchNode(#36)
- **正路径（在线实测 run_4）**：`int` → `切换整型`；`enum` → `切换ECollisionChannel` / `切换EInputEvent`
- **负路径 fail-loud（在线实测 run_4）**：`type_kind 非法 'bogus'`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b1_add_switch_node_int.json` 等

### 5. `add_struct_node`

- **入参**：`(bp_path, graph_name=EventGraph, struct_name, b_is_break, node_pos)`
- **对应 C++ API**：AddStructNode(#37)
- **正路径（在线实测 run_4）**：`make` → `"创建Vector"`；`break` → `"中断Vector"`
- **负路径 fail-loud（在线实测 run_4）**：未知结构 → `返回 None`；空值 → `参数为空`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b1_add_struct_make.json` 等

### 6. `add_enum_literal_node`

- **入参**：`(bp_path, graph_name=EventGraph, enum_name, value_name, node_pos)`
- **对应 C++ API**：AddEnumLiteralNode(#56)
- **正路径（在线实测 run_4）**：`title="文字枚举ECollisionChannel"` ×2
- **负路径 fail-loud（在线实测 run_4）**：空值 → `参数为空`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b1_add_enum_literal_0.json` 等

### 7. `add_composite_node`

- **入参**：`(bp_path, graph_name=EventGraph, node_pos)`
- **对应 C++ API**：AddCompositeNode(#54)
- **正路径（在线实测 run_4）**：`title="EdGraph_0\n合并的图表"`（`pins` 回读为空 → 已留 warning，不静默）
- **负路径 fail-loud（在线实测 run_4）**：图表不存在 → `图表未找到`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b2_add_composite_pos.json` 等

### 8. `add_math_expression_node`

- **入参**：`(bp_path, graph_name=EventGraph, expression, node_pos)`
- **对应 C++ API**：AddMathExpressionNode(#38)
- **正路径（在线实测 run_4）**：`title="(A + B) * C\n数学表达式"` · 653ms
- **负路径 fail-loud（在线实测 run_4）**：空表达式 → `参数为空`；图表不存在
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b2_add_mathexpr_pos.json` 等

### 9. `add_async_action_node`

- **入参**：`(bp_path, graph_name=EventGraph, factory_function_name, factory_class_name, activate_function_name, node_pos)`
- **对应 C++ API**：AddAsyncActionNode(#55)
- **正路径（在线实测 run_4）**：`title="延迟"` · 628ms（ProxyFactory 反射写入）
- **负路径 fail-loud（在线实测 run_4）**：不可解析类 → `拒绝调用引擎`；空类名 → `参数为空`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b2_add_async_pos.json` 等

### 10. `add_create_delegate_node`

- **入参**：`(bp_path, graph_name=EventGraph, function_name, node_pos)`
- **对应 C++ API**：AddCreateDelegateNode(#45)
- **正路径（在线实测 run_4）**：`title="创建事件"` · 590ms
- **负路径 fail-loud（在线实测 run_4）**：空 `function_name` → `参数为空`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b2_add_createdelegate_pos.json` 等

### 11. `add_actor_bound_event_node`

- **入参**：`(bp_path, graph_name=EventGraph, actor_name, delegate_name, node_pos)`
- **对应 C++ API**：AddActorBoundEventNode(#43)
- **正路径（在线实测 run_4）**：`title="Actor开始重叠时 (StaticMeshActor0)"`
- **负路径 fail-loud（在线实测 run_4）**：无该 Actor / 无该委托 → `返回 None`；空委托名 → `参数为空`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b3_add_actorbound_pos.json` 等

### 12. `add_input_axis_event_node`

- **入参**：`(bp_path, graph_name=EventGraph, axis_name, b_is_key_axis, node_pos)`
- **对应 C++ API**：AddInputAxisEventNode(#41)
- **正路径（在线实测 run_4）**：按键轴 `"W"`；命名轴 `"输入轴Move Forward / Backward"`
- **负路径 fail-loud（在线实测 run_4）**：非法键 → `返回 None`（**并自动清理残留**）；空 `axis_name`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b3_inputaxis_key_pos.json` 等

### 13. `add_input_action_event_node`

- **入参**：`(bp_path, graph_name=EventGraph, action_name, node_pos)`
- **对应 C++ API**：AddInputActionEventNode(#40)
- **正路径（在线实测 run_4）**：`title="输入操作Jump"`
- **负路径 fail-loud（在线实测 run_4）**：空 `action_name` → `参数为空`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b3_inputaction_pos.json` 等

### 14. `add_component_bound_event_node`

- **入参**：`(bp_path, graph_name=EventGraph, component_name, delegate_name, node_pos)`
- **对应 C++ API**：AddComponentBoundEventNode(#42)
- **正路径（在线实测 run_4）**：`title="组件开始重叠时 (PywrapBox)"`
- **负路径 fail-loud（在线实测 run_4）**：无该组件 / 无该委托 → `返回 None`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b3_compbound_pos.json` 等

### 15. `add_pin_to_node`

- **入参**：`(bp_path, graph_name=EventGraph, node_guid, direction, pin_type, pin_name)`
- **对应 C++ API**：AddPinToNode(#47)
- **正路径（在线实测 run_4）**：`ok=true`
- **负路径 fail-loud（在线实测 run_4）**：同名引脚 → `引脚 'DupPin' 已存在…（禁静默双建）`；`direction 非法 'sideways'`；GUID 不命中
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b4_add_pin_pos.json` 等

### 16. `remove_pin_from_node`

- **入参**：`(bp_path, graph_name=EventGraph, node_guid, pin_name)`
- **对应 C++ API**：RemovePinFromNode(#48)
- **正路径（在线实测 run_4）**：`ok=true · removed=true`
- **负路径 fail-loud（在线实测 run_4）**：无该引脚 → 报错并回读实读列表
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b4_rmpin_pos.json` 等

### 17. `try_node_add_input_pin`

- **入参**：`(bp_path, graph_name=EventGraph, node_guid, strict)`
- **对应 C++ API**：TryNodeAddInputPin(#49)
- **正路径（在线实测 run_4）**：⚠️ **XFAIL**（引擎层不支持：UE5.1 `K2Node_SwitchInteger` 未实现 `IK2Node_AddPinInterface`）
- **负路径 fail-loud（在线实测 run_4）**：同 unsupported 语义 fail-loud
- **在线结论**：⚠️ xfail
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b4_tryaddpin_pos.json` 等

### 18. `set_node_property`

- **入参**：`(bp_path, graph_name=EventGraph, node_guid, property_name, value)`
- **对应 C++ API**：SetNodeProperty(#31)
- **正路径（在线实测 run_4）**：`NodeComment` / `bCanRenameNode` 均 `ok=true`
- **负路径 fail-loud（在线实测 run_4）**：GUID 不命中；未知属性 → `返回 False`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b4_setprop_NodeComment.json` 等

### 19. `remove_component`

- **入参**：`(bp_path, comp_name)`
- **对应 C++ API**：RemoveComponent(#58)
- **正路径（在线实测 run_4）**：`ok=true`（`removed` 回读不可用 → `warnings[2]`，不静默）
- **负路径 fail-loud（在线实测 run_4）**：组件不存在 → `not found in SimpleConstructionScript`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b5_rmcomp_pos.json` 等

### 20. `add_implemented_interface`

- **入参**：`(bp_path, interface_class_path)`
- **对应 C++ API**：AddImplementedInterface(#59)
- **正路径（在线实测 run_4）**：加固前实测 `ok=true` 330ms（D-1 族入口）
- **负路径 fail-loud（在线实测 run_4）**：不可解析全路径 → `拒绝调用引擎`；重复接口 → `返回 False`；🔴 **裸短名 = R2 硬崩 → R3 已硬化**（R5 真机复验：`BridgeError` 22ms · UE 未崩 · 合法裸短名不误拒）
- **在线结论**：✅（含 R3 硬化）
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b5_addiface_0.json` 等

### 21. `add_event_dispatcher`

- **入参**：`(bp_path, delegate_name)`
- **对应 C++ API**：AddEventDispatcher(#46)
- **正路径（在线实测 run_4）**：`ok=true`
- **负路径 fail-loud（在线实测 run_4）**：同名分发器 → `返回 None（错误文本被 UE 反射吞掉）`；空 `delegate_name`
- **在线结论**：✅
- **raw 锚点**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\b5_adddisp_pos.json` 等

## 2 · 风险与边界注记

| # | 注记 | 依据 |
|:--:|:---|:---|
| 1 | **路径类入参必须可加载**：不可解析的**全路径**与**裸短名** → 前置守卫 `BridgeError`（`拒绝调用引擎`），⛔ 不递引擎（引擎侧 `check()` 断言 = 编辑器硬崩） | R3 Tier B 硬化（§21.1）· R5 真机复验（§22.3） |
| 2 | `try_node_add_input_pin` = **xfail**（引擎层不支持）→ 不得依赖 | 报告 §11.2 用例修正 #6 |
| 3 | `read_nodes`：`graph_name` 有值 → 只返回该图；未指定 → 全量（向后兼容） | R3 §17.3 裁决 + 实施 |
| 4 | `add_implemented_interface` 接口回读在非 GameThread 下**滞后** → 命令按 warning 处理，不阻断 | 报告 §3 第 9 项 |
| 5 | `add_event_dispatcher` 同名失败的引擎错误文本被 UE 反射**吞掉** → 需查 UE 日志 | 报告 §3 第 10 项 |
| 6 | `add_actor_bound_event_node` 依赖**关卡内已存在的 Actor**；`add_component_bound_event_node` 依赖组件已加并编译 | 报告 §3 第 5/8 项 |
| 7 | `add_pin_to_node` 同名引脚 **fail-loud**（禁静默双建）；非法 `direction` 拒绝 | 报告 §2 |
| 8 | M-2 类型铁律：`read_nodes` 返回 `FBPNodeInfo`（非节点）→ 需真实节点须 `get_node_by_guid` | 报告 §4.1 |

## 3 · 复现命令

```
cd skills/ue5-automation
python -m pytest tests/test_pywrap_l2_online.py --ue-online --timeout=240
```

> 前置：UE5.1 编辑器在线 · Bridge `:8889` LISTENING · `X-Skill-Token` 可读；⚠️ 进程内 `ue5_bridge` 若为**旧模块**，fixture 会按**实现指纹**自动热载（`importlib.reload`，不重启 UE、不重编译 DLL）。
