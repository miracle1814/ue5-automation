# UE5 自动化管线 — 陷阱清单（Pitfalls）

> **定位：** UE5 蓝图自动化中已知陷阱的"修复用"速查手册  
> **来源：** `config/error_patterns.json`（**19 条模式 · PTN-001~019**）+ 全员踩坑复盘  
> **格式：** 诊断卡片风格 — 症状 · 根因 · 规避 · 修复  
> **版本：** v1.5 · 2026-10-24（v1.0 初版 2026-07-24 docs-writer；v1.2 · T-20260917-RELEASE-V218 新增 P17/P18；v1.3 · T-20260917-SKILL-CLEANUP-V219 新增 P19 + PTN-016~019 登记闭环；v1.4 · 回家编译实录 B1~B6；v1.5 · 回家真机实录 B7~B12 + B13~B15 §3.6 闭环实录，均不占 PTN 编号）  
> **关联：** `preflight.md` · `quality_gates.md`（仅源端，不随包） · `diagnostic_card_template.md`（仅源端，不随包）

---

## 🔴 阻断级（silent_failure / gc_issue / critical）

### P01 · AssetTools.create_asset 不注册 GeneratedClass

**症状：** 蓝图编译通过但 BeginPlay/Tick 等事件不触发；蓝图无法拖入场景。  
**触发条件：** 使用 `AssetTools.create_asset()` 创建蓝图资产。  
**根因：** `create_asset()` 仅生成 `.uasset` 文件，未注册 GeneratedClass，CDO 类型为 `BlueprintGeneratedClass` 而非实际的 Actor/Widget 类。  
**规避方式：** 始终使用 `factory + create_actor_blueprint`（走 `FKismetEditorUtilities::CreateBlueprint` 原生流程）。  
**严重度：** 🔴 阻断  
**关联模式：** PTN-001 · PTN-003 · PTN-009

```python
# ❌ 错误：AssetTools.create_asset
asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
bp = asset_tools.create_asset(name, path, parent_class)

# ✅ 正确：factory + create_actor_blueprint
factory = unreal.BlueprintFactory()
factory.set_editor_property('parent_class', unreal.Actor)
bp = asset_tools.create_asset(name, path, unreal.Blueprint, factory)
```

---

### P02 · add_function_call_node 需要完整类路径

**症状：** `add_function_call_node` 返回 None，节点未创建，无引脚。  
**触发条件：** 使用短类名调用 Bridge 的 `add_function_call_node`。  
**根因：** Bridge 内部调用 `FindObject<UClass>(nullptr, *TargetClassName)`，需要完整路径格式如 `/Script/Engine.KismetSystemLibrary`。  
**规避方式：** 始终使用 `/Script/<Module>.<ClassName>` 完整路径。  
**严重度：** 🔴 阻断  
**关联模式：** PTN-002 · PTN-007

```python
# ❌ 错误：短类名
bp.add_function_call_node('PrintString', 'KismetSystemLibrary')

# ✅ 正确：完整路径
bp.add_function_call_node('PrintString', '/Script/Engine.KismetSystemLibrary')
```

---

### P03 · CDO 修改在编译前执行导致覆盖

**症状：** 编译后 CDO 默认值被重置；BeginPlay 中设置的 Tick 无效；构造脚本中设置的值编译后丢失。  
**触发条件：** 先修改 CDO 属性再调用 `compile_blueprint()`。  
**根因：** 编译蓝图时 CDO 会重建，覆盖之前通过 `set_editor_property` 设置的值。  
**规避方式：** 调整时序：先 `compile_blueprint()`，再修改 CDO 属性。  
**严重度：** 🔴 阻断  
**关联模式：** PTN-003 · PTN-001 · PTN-009

```python
# ❌ 错误：先改 CDO 再编译
bp.set_editor_property('bCanEverTick', True)
bp.compile_blueprint()  # CDO 被覆盖！

# ✅ 正确：先编译再改 CDO
bp.compile_blueprint()
generated_class = bp.get_editor_property('generated_class')
cdo = generated_class.get_default_object()
cdo.set_editor_property('bCanEverTick', True)
```

---

### P04 · ConnectPins 假阳性 — 报告成功但实际未连接

**症状：** `connect_pins` 返回 True 但蓝图编辑器中节点未连接。  
**触发条件：** 调用 Bridge `ConnectPins` 后不验证，信任返回值。  
**根因：** Bridge C++ 侧 `TryCreateConnection` 是"可行性验证"API，不是"建立连接"API。K2 Schema 类型不匹配时创建隐式 Cast 节点并返回 True，但原始 Pin 之间无 LinkedTo 关系。  
**规避方式：** v0.5 已修复（改用 `MakeLinkTo` 替代 `TryCreateConnection`）。所有连线调用后强制执行 `is_connected` 双向验证。  
**严重度：** 🔴 阻断  
**关联模式：** PTN-004 · PTN-007  
**状态：** ✅ 已修复（v0.5 DLL 345KB）

```python
# 验证模式
result = bp.connect_pins(eg, 'BeginPlay', 'then', 'PrintString', 'execute')
pins = bp.list_pins(eg, 'PrintString')
assert pins['execute'].get('connected', False), 'connect_pins 假阳性！'
```

---

### P05 · Python 端节点引用跨调用丢失（GC 回收）

**症状：** 第一次 `ue_send.py` 调用创建的节点引用，第二次调用时变为 None。  
**触发条件：** 跨多次 `ue_send.py` 调用分步操作蓝图节点。  
**根因：** Python 端创建的 UObject 引用无 UPROPERTY 保护，未注册到 UE GC 追踪系统。  
**规避方式：** 将所有创建、连线、验证操作合并到单次 `ue_send.py` 调用中。  
**严重度：** 🔴 阻断  
**关联模式：** PTN-005 · PTN-006

```python
# ✅ 正确：单次调用完成所有操作
script = '''
eg = bp.get_event_graph()
begin = bp.add_event_node(eg, 'BeginPlay')
ps = bp.add_function_call_node('PrintString', '/Script/Engine.KismetSystemLibrary')
bp.connect_pins(eg, 'BeginPlay', 'then', 'PrintString', 'execute')
bp.compile_blueprint()
'''
```

---

### P06 · 成员指针无 UPROPERTY 保护导致 GC 野指针

**症状：** 运行时随机崩溃（Access Violation 0x00000000…）。  
**触发条件：** C++ 类中 UObject* 成员变量未加 UPROPERTY() 宏。  
**根因：** C++ 成员变量 `UObject*` 指针或 `TArray<UObject*>` 未加 `UPROPERTY()` 宏，UE GC 无法追踪。  
**规避方式：** 所有 UObject* 成员必须加 `UPROPERTY()` 宏。  
**严重度：** 🔴 阻断  
**关联模式：** PTN-006 · PTN-005

```cpp
// ❌ 错误：无 UPROPERTY
UMyActor* TargetActor;
TArray<UObject*> Objects;

// ✅ 正确
UPROPERTY()
TObjectPtr<UMyActor> TargetActor;
UPROPERTY()
TArray<TObjectPtr<UObject>> Objects;
```

---

### P10 · 引用资产丢失 — Referenced asset is null

**症状：** 蓝图节点上引用资产显示为空；日志报 `Referenced asset is null`。  
**触发条件：** 资产路径变更、被移动/删除/重命名。  
**根因：** 软引用未正确解析或资产物理路径已变更。  
**规避方式：** 使用 Asset Registry 扫描匹配名称自动替换路径；资产已删则标记待清理。  
**严重度：** 🔴 阻断  
**关联模式：** PTN-010 · PTN-012

```python
# 自动修复引用路径
import unreal
reg = unreal.AssetRegistryHelpers.get_asset_registry()
candidates = reg.get_assets_by_class('BehaviorTree', search_sub_classes=True)
```

---

### P12 · 循环依赖 — 蓝图 A ↔ B 相互引用

**症状：** 两个或多个蓝图相互 Cast / SpawnActor / 硬引用，形成依赖环。  
**触发条件：** 蓝图 A 引用 B 的同时 B 也引用 A（或通过中间蓝图形成环）。  
**根因：** 双向硬引用导致编译/加载时依赖解析失败或卡死。  
**规避方式：** 用 Interface 替代直接 Cast；用 Event Dispatcher 替代直接函数调用；抽取公共依赖到第三个蓝图（Mediator）。  
**严重度：** 🔴 阻断  
**关联模式：** PTN-012 · PTN-013

**修复步骤：**
1. 确定环上所有蓝图
2. 选择断开最弱的边（通常是 Cast 关系）
3. 用 Interface（BPI_XXX）替代 Cast
4. 或引入中间 Managers/Subsystems

---

## 🟡 降级/警告级（compile_error / api_usage / node_error）

### P07 · 引脚名猜测导致 connect_pins 失败

**症状：** `connect_pins` 报 'pin not found'；实际引脚名与预期不符。  
**触发条件：** 连线前未确认节点实际引脚名。  
**根因：** 引脚名因 UE 版本/蓝图类型而异。纯函数无 exec 引脚。  
**规避方式：** 连线前强制调用 `list_pins` 获取实际引脚名。  
**严重度：** 🟡 警告  
**关联模式：** PTN-007 · PTN-004 · PTN-002

```python
# ✅ 安全流程
pins = bp.list_pins(eg, 'PrintString')
print(pins)  # 确认实际引脚名
# 然后根据实际名称连线
```

---

### P08 · 批量操作失败后半成品蓝图残留

**症状：** 批量创建蓝图时部分失败，成功创建的一半蓝图留在 Content Browser 中。  
**触发条件：** SafeWrapper 批量模式执行中途失败。  
**根因：** 批量模式无事务机制。失败后未执行 `_cleanup_orphan_blueprint()`。  
**规避方式：** SafeWrapper 集成 `_cleanup_orphan_blueprint()` 自动清理。  
**严重度：** 🟡 警告  
**关联模式：** PTN-008 · PTN-014

> ⚠️ SafeBatch 批量执行器尚未实现。当前建议：单蓝图逐一创建，每次创建后编译验证并手动检查残留。

---

### P09 · 蓝图编译失败 — 变量类型未找到

**症状：** 蓝图编译报错：`Variable 'XXX' type 'TArray<FXXX>' not found`。  
**触发条件：** 蓝图引用的结构体/枚举/类未在 Content Browser 中正确保存或路径变更。  
**根因：** 蓝图引用的类型资产路径变更或资产被删除。  
**规避方式：** 检查 Content Browser 中对应结构体是否存在；存在则 Refresh Node；不存在则重建。  
**严重度：** 🟡 警告  
**关联模式：** PTN-009 · PTN-011

**修复步骤：**
1. 打开 Content Browser，搜索变量类型名
2. 存在 → 右键蓝图 → Refresh All Nodes
3. 不存在 → 创建 Structure 资产，添加所需字段
4. 重新编译蓝图

---

### P11 · 接口函数未实现 — Interface function not implemented

**症状：** 蓝图实现了接口但缺少某个接口函数的实现，编译报错。  
**触发条件：** 蓝图声明实现某 Interface，但事件图中缺少对应的 Function Override 节点。  
**根因：** Interface 升级后新函数未被蓝图实现。  
**规避方式：** 自动在蓝图事件图中添加缺失接口函数的 Function Override 节点 + 空实现。  
**严重度：** 🟡 警告（可自动修复）  
**关联模式：** PTN-011 · PTN-009

```python
# 自动补全接口函数
bp_interface = unreal.load_asset(interface_path)
for func in bp_interface.get_functions():
    if not bp.has_function_override(func.get_name()):
        bp.add_function_override(func.get_name())
```

---

### P13 · Tick 耗时过长 — 性能告警

**症状：** 帧率下降、蓝图 Tick 中执行重计算。  
**触发条件：** Tick 中执行 O(n) 或更重的操作；300+ 节点大蓝图。  
**根因：** 未用 Timer 替代高频 Tick。  
**规避方式：** 将重计算移出 Tick → 用 Timer/Event 驱动；大数据量用 ParallelFor 或 AsyncTask。  
**严重度：** 🟡 警告  
**关联模式：** PTN-013 · PTN-012

**修复步骤：**
1. 用 `stat unit` / `stat game` 定位卡顿来源蓝图
2. 评估 Tick 中的操作是否可以降低频率（Timer 替代）
3. 遍历类操作考虑空间分区（Grid/Hash）减少搜索范围
4. 非 GameThread 操作考虑 AsyncTask/ParallelFor

---

### P14 · 蓝图节点已废弃 — Node deprecated

**症状：** 蓝图编译/打开时警告：某个节点已废弃，建议使用新 API 替换。  
**触发条件：** UE 引擎版本升级后部分蓝图节点/数据类型被标记为 Deprecated。  
**根因：** 引擎 API 演进导致旧节点路径废弃。  
**规避方式：** 查阅 UE Release Notes 找到替代节点，手动替换并重新连线。  
**严重度：** 🟡 警告  
**关联模式：** PTN-014 · PTN-008

**修复步骤：**
1. 定位废弃节点（编译日志会给出节点名+位置）
2. 查阅 UE 文档/Release Notes 找到替代节点
3. 手动替换节点并重新连线

---

### P15 · clear_graph_nodes 残留引用

**症状：** `clear_graph_nodes` 后旧节点的引脚引用未释放；新节点创建时引脚冲突。  
**触发条件：** 调用 `clear_graph_nodes` 后重建蓝图。  
**根因：** `clear_graph_nodes` 内部未调用 `DestroyNode()`，仅从数组中移除节点引用但未通知 UE 编辑器节点已销毁。  
**规避方式：** C++ 侧修复：`clear_graph_nodes` 内部对每个节点调用 `DestroyNode()` 再移除。Python 侧验证：clear 后 `list_nodes` 确认归零。  
**严重度：** 🟡 警告  
**关联模式：** PTN-015 · PTN-008 · PTN-004

```cpp
// C++ 侧修复（需重建 Bridge DLL）
void UBPBridge::ClearGraphNodes(UEdGraph* Graph) {
    for (UEdGraphNode* Node : Graph->Nodes) {
        Node->DestroyNode();  // ⚠️ 必须调用
    }
    Graph->Nodes.Empty();
}
```

---

### P16 · 关键调用链上的功能 stub

**症状：** 模块的关键入口函数（如 `reader.analyze()` → `_exec_embedded()`）实现为 `{return False; / pass / TODO}`，调用方未感知到功能缺失，运行到该路径时静默失败或无输出。  
**触发条件：** 开发阶段预留的未实现路径直接合入生产代码，入口函数存在但调用链末端为空壳。  
**根因：** stub 代码未被识别为生产阻塞项，缺少显式的未完成标记（如 `raise NotImplementedError`）或门禁检测。  
**规避方式：** 代码审计时检查每个公开 API 的调用链末端是否存在功能 stub；门禁脚本自动扫描 `pass`/`TODO`/`return False` 等模式并标记为阻断。  
**严重度：** 🔴 致命  
**关联模式：** PTN-016 · PTN-004

**修复方案：**
1. **完整实现** — 按需求规格完成缺失功能路径
2. **显式阻断** — 若暂不具备实现条件，在入口处显式 `raise NotImplementedError("功能路径 XXX 未实现")`，避免静默失败
3. **门禁扫描** — 将 stub 检测纳入 CI/CD 门禁：扫描 `pass` / `TODO` / `return False` 在公开 API 调用链中的出现

```python
# ❌ 危险：stub 无标注，调用方无感知
def _exec_embedded(self, node_data):
    pass  # TODO — 但未被门禁检测到

# ✅ 正确：显式阻断
def _exec_embedded(self, node_data):
    raise NotImplementedError(
        "_exec_embedded: 嵌入式节点执行路径未实现，"
        "需完成 node_data → 蓝图节点映射逻辑。"
        "参见 pitfalls.md P16。"
    )
```

---

### P17 · `/build` 事件存在性判定误判 → 事件节点静默漏建

**症状：** `POST /build` 返回 `success=true`、无 `error`、steps 无失败项，但目标事件节点**根本没建**（蓝图少了事件入口，编译照样通过、逻辑永不触发）。
**触发条件：** spec 要的事件（如 `ReceiveActorEndOverlap`）与图中脚手架自带事件的**本地化标题**发生"无关命中"。
**根因：** 模板旧候选表把**全部**事件的本地化标题无条件加入候选（`_localized_map.values()`），与目标 `event_name` **无语义绑定** → 「事件Actor开始重叠」（= `ReceiveActorBeginOverlap`）被当作 `ReceiveActorEndOverlap` 命中 → 判 `exists` → 静默跳过创建。
**规避方式：** ① 事件存在性判定必须**语义键绑定**（`_norm_event_key()`：去 `event`/`receive` 前缀 + 去 `_`/空格 + 小写），非本事件标题**绝不入候选**；② 判 `exists` 必须回传**判据**（`matched_by` / `matched_title` / `candidates`）供人工复核；③ 映射表外的事件名退化为「候选=[原名] → 建不出即 fail-loud」，**禁止静默吞掉**。
**严重度：** 🔴 阻断（silent_failure）
**修复状态：** ✅ 已修复（`T-20260916-UE5AUTO-HALLBEACON-D1D4-FIX` · `game_thread_build.py.tmpl`；离线回归 6 例，修复前 4 例红）

```python
# ❌ 错误：所有事件的本地化标题都当候选（与 ev_name 无语义绑定 → 误判 exists）
for _title in _localized_map.values():
    _event_title_candidates.append(_title)

# ✅ 正确：只保留与目标事件同语义键的标题
for _map_key, _map_title in _localized_map.items():
    if _norm_event_key(_map_key) != _ev_norm:
        continue
    _event_title_candidates.append(_map_title)
```

---

### P18 · 同名节点仅按标题定位 → 无法分别设值（须按 GUID）

**症状：** 蓝图里有多个同名节点（如 3×「设置Actor在游戏中隐藏」），`set_pin_default_value` / `get_pin_default_value` 只能命中其中之一，其余**无法单独设值**（想分别设 true/false/true 做不到）。
**触发条件：** 仅用**节点标题**定位，且图中存在同名节点。
**根因：** 定位通路单一（`_find_bp_node_info` + `_find_node_by_title_i18n`），**标题在同名场景不唯一** → 无稳定锚点。
**规避方式：** ① 同名节点必须**按 GUID 定位**（`get_node_by_guid` / `_pywrap_find_graph_by_guid` → C++ `GetNodeByGuid`，精确唯一）；② Python 侧 `node_id` 同时接受标题与 32 位 GUID（`_pywrap_is_guid_like` 判定），并可用显式别名 `node_guid`；③ 返回体携带 `matched_by` ∈ {`guid`,`title`}，调用方可确认命中依据；④ 取到 GUID 后喂 `get_pin_infos` 必须用 `FBPNodeInfo.raw` 原始结构体（传包装对象会 `AttributeError` → 被 except 转成假阴性）。
**严重度：** 🟡 警告（场景受限 / 静默错值）
**修复状态：** ✅ 已修复（`T-20260916-UE5AUTO-HALLBEACON-D1D4-FIX` · `ue5_bridge.py`；**零 DLL 改动**，复用既有 pywrap 通路）

```python
# ❌ 错误：同名节点靠标题定位（命中哪个不确定）
{"command": "set_pin_default_value",
 "params": {"node_id": "设置Actor在游戏中隐藏", "pin_name": "bHidden", "new_value": "true"}}

# ✅ 正确：按 32 位 GUID 精确锚定（GUID 来自 get_node_by_guid / read_nodes 回读）
{"command": "set_pin_default_value",
 "params": {"node_id": "<32位GUID>", "pin_name": "bHidden", "new_value": "true"}}
```

---

### P19 · `/build` spec 字段名契约与实现不一致（events / functions）

**症状：** 第三方**照文档写 spec** → 节点建不出来，且报错把根因指向**错误方向**。events 写 `name` → 得**空事件名** + `add_event_node returned None`；functions 写 `{"name":…, "target_class":…}` → 得**空函数名** + `add_function_node returned None`，错误文本却提示「① 函数名不存在 ② 该类不含此函数 ③ 非 UFUNCTION ④ 类路径拼写/大小写错误」—— 四条**全不对**，真实原因是**键名写错**。
**触发条件：** 使用文档（`ue5_runner.build()` docstring / `BuildRequest` 注释 / SOP）声明的主字段名，而非实现实际读取的键名。
**根因：** 文档与实现**各说各话**且 bridge 侧**原样透传**未做归一化 —— events：文档 `name` vs 实现 `event_name`；functions：文档 `name` + `target_class` vs 实现 `function_name` + `function_path`。属**契约层**缺陷（不是调用方写错）。
**规避方式：** ① bridge 侧**归一化**（`_normalize_build_events` / `_normalize_build_functions`）：**主字段优先 + 别名兼容 + 冲突取主字段并回传 `warnings`**；② **两者皆缺 → fail-loud**，错误点名 `events[i]` / `functions[i]` 并回传**实收 keys**（⛔ 不得再落「空名 + returned None」式误导）；③ 模板侧**双层兼容**；④ 文档与实现对齐 —— 契约 schema 唯一权威源 = `docs\SOP-PIPELINE.md` §3.1。
**严重度：** 🟡 错误（影响照文档写 spec 的第三方使用者）
**修复状态：** ✅ 已修复（D-5 · `T-20260917-D5D6-CONTRACT-FIX` events；D-8 · `T-20260917-D8-FUNCTIONS-CONTRACT` functions；均零 DLL 改动）

```python
# ❌ 错误（文档写的键，实现不认）—— events 得空事件名 / functions 得空函数名
{"events":    [{"name": "ReceiveActorEndOverlap"}],
 "functions": [{"name": "PrintString", "target_class": "/Script/Engine.KismetSystemLibrary"}]}

# ✅ 正确：主字段（两者等价可用，同时给出且冲突时取主字段 + warnings 告警）
{"events":    [{"event_name": "ReceiveActorEndOverlap"}],          # name 为兼容别名
 "functions": [{"function_name": "PrintString",
                "function_path": "/Script/Engine.KismetSystemLibrary:PrintString"}]}
```

---

## 🟠 构建期陷阱（回家编译实录 2026-10-24 · B1~B6）

> `feat/p0-toolkit` C++ 桥（BlueprintCapture.cpp 截图闭环）回家编译实测踩到并已修复的构建期陷阱。
> 与蓝图运行时无关，**不占 PTN 编号**；逐处修复细节见 BlueprintCapture.cpp 头部「TODO(home) 编译排错记录」。
> 实测环境：UE 5.1（VS2019 14.29）+ UE 5.8（VS2022 MSVC 14.44）双引擎编译通过。

### B1 · 仓库路径含中文 → MSVC C1083（PCH 中间文件路径乱码）

**症状：** UBT 报 `fatal error C1083: 无法打开编译器中间文件 "...SharedPCH...pch"`，日志中项目路径呈 GBK 乱码（`ue5-鍥炲浠...`）。
**根因：** 系统码页 GBK 与 MSVC/UBT 路径编码不一致；UE 官方本就要求引擎与项目路径纯 ASCII。
**规避方式：** 仓库一律 clone 到纯 ASCII 路径（如 `<盘符>/ue5-automation`）。
**严重度：** 🔴 阻断

### B2 · 用户级 BuildConfiguration.xml 钉死 VS2019 → 新引擎找不到工具链

**症状：** 编 5.8 报 `Unable to find valid 14.29.30133 C++ toolchain for VisualStudio2026 x64`（21 秒即失败）。
**根因：** `%APPDATA%\Roaming`、`Documents`、`%APPDATA%\Local` 三份用户级 `UnrealBuildTool\BuildConfiguration.xml` 写死 `<CompilerVersion>14.29.30133</CompilerVersion>`（早年为 5.1 稳定性所设，bat 注释里的 Win32Exception 998 处方）。
**规避方式：** 编多版本引擎时临时挪开三份配置（`.bak`）→ 编译 → 恢复；长期应按引擎版本分开管理。
**严重度：** 🟡 高

### B3 · UAT BuildPlugin 暂存路径与 build_for_engine.bat 预期不符

**症状：** bat 报 `UnrealEditor.modules not found next to the built DLL`（DLL 找得到、modules 找不到）。
**根因：** UAT 实际暂存到 `Out\Binaries\Win64\`，bat 找的是 `Out\<PluginName>\Binaries\Win64\`；modules 不同步会让 UE 拒载插件（`Incompatible or missing module`，v3.2 注释明确过的坑）。
**规避方式：** 编译后从 `Out/Binaries/Win64/` 手动补拷 modules（本次已如此部署 5.1+5.8）；bat 的 staging 路径修正列为待办。
**严重度：** 🟡 高

### B4 · 离线草稿 include 漂移（路径错 + 漏 include，编译期才暴露）

**症状与修复（均已修正，双引擎验证）：**
- `Engine/WorldContext.h` 不存在 → `FWorldContext` 内联在 `Engine/Engine.h`（5.1 第331行 / 5.8 第351行）
- `GameFramework/PlayerCameraManager.h` → 实际在 `Camera/`
- `GameFramework/GameViewportClient.h` → 实际在 `Engine/`
- `Interfaces/IImageWrapper.h` → 本机引擎头文件直接在 `Runtime/ImageWrapper/Public/` 根（无 `Interfaces/` 子目录）
- `LevelEditorViewportClient.h` 不存在 → 类在 `LevelEditorViewport.h`；`GCurrentLevelEditingViewportClient` 声明在 `Editor.h`
- 漏 include：`EngineUtils.h`（TActorIterator）、`Async/Async.h`（EAsyncExecution/Async）
- 辅助函数区少一个 `#endif`（C1070 失配）
**规避方式：** 离线写完 C++ 后，先 `find <Engine>/Source -name "*.h"` 建索引、把草稿全部 include 批量核对一遍再编译，一轮清光路径错。
**严重度：** 🟡 高

### B5 · FScreenshotRequest::RequestScreenshot 无 Size 重载

**症状：** C2665 两个重载均不匹配。
**根因：** 实际签名 `(Filename, bShowUI, bAddFilenameSuffix)`，草稿按记忆传了 `FIntPoint(W,H)` 当第 3 参。
**规避方式：** 以引擎头文件为准（`UnrealClient.h:152` 起）；Filename 绝对路径是否被引擎重定向仍待真机验证（BlueprintCapture.cpp 内 TODO 保留）。
**严重度：** 🟡 中

### B6 · 仪表盘 `?token=` 服务端支持、前端从未解析

**症状：** 浏览器打开 `/ui?token=xxx` 恒显「未授权」，须手动粘贴 token。
**根因：** `ui/index.html` 只从 localStorage 读 token，无任何 URLSearchParams 解析——而页头注释与服务端注释都承诺了该行为。DOM 契约测试（字符串断言）覆盖不到流程级缺陷，**真浏览器实测才能发现**。
**规避方式：** 已修（URL 参数优先 + 记住本机）；顺带修掉连接状态硬编码 `:8889`（改 `location.host`，`UE5_BRIDGE_PORT` 覆盖时显示才正确）。
**严重度：** 🟡 中

### B7 · UE 矩阵是行向量约定 —— 轴重映射矩阵写成转置

**症状：** 投影修复第一版（插入轴映射矩阵）真机复测仍全 invisible，dead-center 瞄准也判出画。
**根因：** UE `FMatrix` 是**行向量约定（P·M）**，按列向量直觉写的映射矩阵实际做了 (x,y,z)→(z,x,y) 转置换轴。行向量语义下正确映射：M[0]=(0,0,1,0)、M[1]=(1,0,0,0)、M[2]=(0,1,0,0)。
**规避方式：** 跨约定手写矩阵时，先用引擎真值单点对照（官方 MCP `EditorAppToolset.WorldPosToScreenCoords`，本次 NDC 误差 <1e-3 定案），别靠记忆推。
**严重度：** 🔴 阻断（修前投影全错）

### B8 · `unreal.*` 只能 GameThread 调 —— HTTP 线程连 get_engine_version 都抛异常

**症状：** /health 里 engine_version/project_dir 全 "unknown"；画面面板恒 404（截图明明在盘上）。
**根因：** `_get_ue_info` 与 `ui_dashboard._screenshots_dir` 在 uvicorn 线程直调 unreal.*，全部抛异常被吞成 "unknown"/""——桥的 GT 纪律对"纯路径/元数据"调用同样生效。
**规避方式：** 已修：元数据读取统一走 `_run_on_game_thread_sync`（同 pie_start 口径）；ui_dashboard 不许反向 import bridge（成环）→ 由 `start()`（init_unreal 在 GT 上调）注入截图目录（`set_screenshots_dir`），注入失败 fail-open 退占位。
**严重度：** 🟡 高

### B9 · `unreal.Paths.*` 恒返回相对路径

**症状：** project_dir 形如 `../../../../ProjName/`；`os.path.isdir` 相对进程 CWD（引擎 Binaries）解析必然失败。
**规避方式：** 一律 `unreal.Paths.convert_relative_path_to_full(...)` 再用。
**严重度：** 🟡 中

### B10 · pie_start 在 UE 5.8 API 改名

**症状：** `editor_play_in_editor` 跨三库全 missing（5.1 可用）。
**根因：** 5.8 `LevelEditorSubsystem.h:78` 改为 `EditorRequestBeginPlay()`（停止 `EditorRequestEndPlay()`、状态 `IsInPlayInEditor()`）；旧名移除。
**规避方式：** 候选名单按新引擎优先带新旧两代（`editor_request_begin_play` → `editor_play_in_editor`）；改 API 前先 grep 引擎头文件拿权威名。
**严重度：** 🟡 高

### B11 · StartPIE 刚返回后的首张截图会超 2s 轮询

**症状：** capture_pie 报「截图文件不存在」，文件几秒后自己出现在正确路径。
**根因：** PIE 预热（shader 编译等帧卡顿）+ FScreenshotRequest 延迟一帧 + GT 端 PNG 编码，首张可超 2s；绝对路径**没有**被引擎重定向（TODO(home) 担忧证伪）。
**规避方式：** 稳态保持规格 2s，capture_pie 经 `_pending_poll_timeout` 放宽 8s。
**严重度：** 🟡 中

### B13 · UFUNCTION 从非 GT 线程调用被 5.8 引擎直接拒绝（9 月旧 DLL 无此断言）

**症状：** /build 端点 RuntimeError "Attempted to access Unreal API from outside the main game thread"。
**根因：** build_blueprint 等经 C++ 桥的 ExecutePythonOnGameThread（其内部 AsyncTask 入 GT）派发脚本；5.8 起 UFUNCTION 派发层本身就拒绝非 GT 调用——跳板在入口就被拦，9 月旧 DLL 无断言所以从未暴露。
**规避方式：** 已修：新增 `_gt_exec_python`（经本模块 GT 队列直呼 exec，语义不变，C++ 跳板退役）；TEST_MODE 分支保留旧跳板（D8 契约测试的 mock 依赖它）。
**严重度：** 🔴 阻断（5.8 上 /build 全挂）

### B14 · 新建节点的 node_title 在 5.8 DLL 裸包装上不可达（恒 ''，NodeGuid 同族）

**症状：** apply_graph 每条新连线都被误判"未在拓扑中确认"，读回验证全线报"连线缺失"；但图实际已写好（引脚级回读通过）。
**根因：** DLL 返回的裸 K2Node 包装器上 node_title 属性不可读（与 H-1 的 NodeGuid 不可达同族）；离线 fake 恒有标题所以测试全绿。
**规避方式：** 已修：连线确认在标题取不到时跳过拓扑比对（引脚级已证）；读回验证改用「读回节点的本地化标题」（GUID 映射），映射不到降级引脚级 + 显式告警。
**严重度：** 🔴 阻断（apply_graph 恒回滚/恒报失败）

### B18 · UE 事务 API 三个语义坑（`GetUndoCount` 不是"可撤销数"）

**症状（任务级撤销首版）：** 写命令明明在事务里执行，状态却报 `undo_count=0`；
事务确实存在时 `next_title` 又恒为空 → 回滚按钮永远不可用。

**根因（5.1/5.8 头文件 + 真机双重实证）：**
1. `UTransactor::GetUndoCount()` 的注释是"number of transactions that were undone
   and can be **redone**" —— 它是**已撤销/可重做**条数；可撤销数 =
   `GetQueueLength() - GetUndoCount()`。按字面理解必错。
2. `GetUndoContext(false)` 在 5.1 事务刚结束时返回**空 context**（传 false 也不给
   标题）；下一步撤销的标题要从队列末位取：
   `GetTransaction(GetQueueLength()-1)->GetTitle()`。
3. 队列索引方向（SUndoHistory.cpp:355 实证）：`[0, len-undoCount)` 是重做区，
   末尾 `len-undoCount` 个才是可撤销 —— 下一步撤销在**末位**，不是 0。

**规避方式：** `BlueprintTransaction.cpp` 封装 `AgentTxUndoableCount()` /
`AgentTxNextUndoTitle()`，所有判断只经这两个函数；验收判据同时看
`undo_count`（可撤）、`redo_count`（可重做）、`agent_undoable`（栈顶连续 agent 步）。
**严重度：** 🔴 阻断（功能完全不可用，且表面风平浪静）

### B19 · UE 5.8 不再执行项目自身 Content/Python/init_unreal.py

**症状：** 5.8 项目启动后插件 DLL 已加载（`/diag` 可查），但 8889 端口没有监听；
5.1 同款项目布局却正常自启。

**根因：** UE 会为每个启用插件运行其 `Content/Python/init_unreal.py`，但**不再运行
项目自身** `Content/Python/init_unreal.py`（5.8 起）。项目级 init 是 5.1 时代的
自启方式，5.8 静默失效。

**规避方式：** 5.8 的桥启动挂在 `Plugins/ue5-automation-extras/Content/Python/
init_unreal.py`（插件 init 是 5.8 唯一自动入口）；`ue5_bridge.start()` 幂等
（端口已监听则直接返回），重复调用无害。5.1 仍可用项目级 init 或插件 init。
**严重度：** 🔴 阻断（"装了插件没反应"，且没有任何报错）

### B17 · 收尾演练三项定案（数值→bool 不可连 / 标题定位 / 最小化 PIE）

**① float/int → bool 引脚在写入层被拒（不是缺陷）：**
`ApplyGraph` 对 `float→Branch.Condition` 报 `ConnectPins 失败: 引脚不存在或类型不可连`，
事务 `rollback.clean=true` 全量回滚。UE 的 bool 引脚**不接受数值隐式转换**
（数值互转才有转换节点）；若要"坏图"演练，请用可写入但编译失败的类型组合
（如 int→String，会成功连上并由编译器报"不能连接引脚…Convert 整数 to 字符串"）。
**判据修正：** 这类 spec 的正确预期是"写入层 fail-loud + 干净回滚"，不是 BS_Error。

**② 连线/断线按标题定位时，节点标题 ≠ target：**
`apply_graph` 的 spec `target` 是函数名/变量名（如 `Score`），图内节点标题带前缀
（`Set Score` / `Get Score` / `Print String` / `事件BeginPlay`，且随语言本地化）。
按 target 调 `disconnect_pin` 会"节点未找到"。**正解**：先 `search_blueprint_nodes`
拿图内真实标题（或歧义时给的 GUID），再定位；同名歧义用 GUID。

**③ 最小化窗口的 PIE 截图：fail-loud 已达成，错误原因可能是"PIE 未运行"：**
窗口最小化后 PIE 帧不推进 → 引擎侧直接判定会话不可用，`capture_pie` 报
「PIE/Simulate 未在运行」而非 2s/8s 超时。两者都满足"绝不返回白图/假成功"。
**判据修正：** 断言"任何 fail-loud"而非特指超时文案。
另观察到：最小化 + PIE 组合下编辑器可能自行退出（1 次复现，未深挖）——
长时间无人值守建议保持窗口可见。

### B16 · bBeingCompiled 等 transient 位域不暴露给 Python 反射（守卫不可用的定案）

**症状：** apply_graph 恒带告警「bBeingCompiled 不可读→编译中守卫未生效」。
**根因（debug_reflect 真机探针定案）：** 该标志是 `UPROPERTY(transient)` 位域且在 WITH_EDITORONLY_DATA 内；5.1/5.8 的蓝图 Python 包装器**完全不暴露**（dir() 无任何 compil 成员，get_editor_property/直取四种写法全失败；status 则是 protected）。纯 Python 守卫在此两引擎不可实现，需 C++ 通道（未来增强：桥加 IsBlueprintCompiling）。
**实际防线：** 全部写操作与编译已走 GT 队列**串行化**——同线程任务不可能交错，标志守卫本就只是防「外部触发的异步编译」（如用户同时手点编译），属纵深防御。
**规避方式：** 告警文案已按实情更新；新增只读命令 debug_reflect（资产反射探针，84 号命令）沉淀本次排查方法。
**严重度：** 🟡 低（有 GT 串行化兜底）

### B15 · UE 5.8 中文引擎的编译错误行格式 + 日志异步落盘延迟

**症状：** ①compile_and_diagnose 对真实编译失败返回 errors:[]；②紧跟 apply 的诊断也扑空（compiled=False 但 errors=[]）；③修复收敛后 errors 仍含上一轮旧错。
**根因：** ①两版真实格式都是 `LogBlueprint: Error: [AssetLog]...`（老正则认的 `LogBlueprintUserMessages: [ctx] msg` 实测两版均未出现）：5.8 无空格+全角冒号+`[编译器]`；5.1 带空格+`(no disk path found)` 附加段+半角冒号+`[Compiler]`——正则已泛化四处差异，双引擎真机各提取 2 条验证；②UE 日志异步刷盘，毫秒级连续调用时错误行尚未落盘；③tail-200 窗口天然含历史行（契约：errors 与编译结论独立，收敛看 compiled）。
**规避方式：** 已修：①新增 V58 行变体正则（资产取自 [AssetLog]，中英引擎双支持）；②编译失败且零提取 → sleep 2s 重读一次再提取。③属契约内行为，SOP 判据用 compiled。
**v4.6 进展：** 主路径改为 `compile_blueprint_structured`（直读 FCompilerResultsLog token，不依赖日志刷盘、不靠标题反查 GUID）；本条的日志正则保留为旧 DLL 回退路径。
**严重度：** 🔴 阻断（诊断闭环三处连环坑）

### B12 · Pillow 在编辑器进程内 import 失败（standalone 同解释器正常）

**症状：** 引擎 `python.exe` 里 `from PIL import Image` 正常（9.5.0），编辑器内同解释器必失败 → 标注跳过+告警（设计降级，不失败）。
**根因（推断）：** UE 进程已加载同名依赖 DLL（zlib1.dll 等），PIL 的 pyd 绑定到已加载的不兼容副本——UE+Pillow 经典坑。
**规避方式：** 待办：换 UE 兼容的 Pillow 构建/改名依赖 DLL；现状由「无 Pillow → PNG 魔数+IHDR 结构化读回」兜底（真机已自证）。
**严重度：** 🟡 低（功能降级可用）

---

## 附录：陷阱 → 错误模式映射

| 陷阱 ID | 错误模式 | 严重度 | 自动修复 |
|:---|:---|:---:|:---:|
| P01 | PTN-001 · AssetTools 不注册 GeneratedClass | 🔴 | ✅ |
| P02 | PTN-002 · add_function_call_node 需要完整路径 | 🔴 | ✅ |
| P03 | PTN-003 · CDO 修改被编译覆盖 | 🔴 | ✅ |
| P04 | PTN-004 · ConnectPins 假阳性 | 🔴 | ✅ 已修复 |
| P05 | PTN-005 · Python 节点引用 GC 回收 | 🔴 | ✅ |
| P06 | PTN-006 · 无 UPROPERTY GC 野指针 | 🔴 | ❌ |
| P07 | PTN-007 · 引脚名猜测失败 | 🟡 | ❌ |
| P08 | PTN-008 · 批量操作半成品残留 | 🟡 | ✅ |
| P09 | PTN-009 · 编译变量类型未找到 | 🟡 | ❌ |
| P10 | PTN-010 · 引用资产丢失 | 🔴 | ✅ |
| P11 | PTN-011 · 接口函数未实现 | 🟡 | ✅ |
| P12 | PTN-012 · 循环依赖 | 🔴 | ❌ |
| P13 | PTN-013 · Tick 耗时过长 | 🟡 | ❌ |
| P14 | PTN-014 · 节点已废弃 | 🟡 | ❌ |
| P15 | PTN-015 · clear_graph_nodes 残留引用 | 🟡 | ✅ |
| P16 | PTN-016 · 关键调用链上的功能 stub | 🔴 | ❌ |
| P17 | PTN-017 · `/build` 事件存在性误判 → 事件节点静默漏建 | 🔴 | ✅ 已修复 |
| P18 | PTN-018 · 同名节点仅按标题定位 → 无法分别设值（须按 GUID） | 🟡 | ✅ 已修复 |
| P19 | PTN-019 · `/build` spec 字段名契约与实现不一致（events / functions） | 🟡 | ✅ 已修复 |

---

> **pitfalls.md v1.4** · 19 条蓝图陷阱 + 19 条构建实录（B1–B19） · 来源：`config/error_patterns.json`（19 条模式 · PTN-001~019）+ 全员踩坑复盘（P16–P19 为复盘新增，**PTN 编号已登记**：PTN-016/017/018/019）  
> **关联：** `preflight.md` · `quality_gates.md`（仅源端，不随包） · `diagnostic_card_template.md`（仅源端，不随包）
