# BlueprintAnalysis 输出 Schema 文档

> **数据源：** `blueprint_reader.py` (v0.6)  
> **适用版本：** UE **5.1.x**（随包 C++ Bridge DLL 828,416B / 59 UFUNCTION 仅实测 5.1.x；其他版本见根目录 `UE_VERSION_GUIDE.md`）  
> **生成日期：** 2026-07-24  
> **对应数据类定义：** `skills/ue5-automation/scripts/blueprint_reader.py` 第 62-176 行

---

## 一、枚举：DegradeLevel（三级降级）

| 值 | 名称 | 含义 | 数据完整度 |
|:---|:---|:---|:---|
| `L1_NATIVE` | UE 原生 Python API | EditorAssetLibrary + KismetSystemLibrary | 全部字段可用 |
| `L2_BRIDGE` | C++ BlueprintPythonBridge | GetNodeConnections / GetPinInfos / ListNodes | 节点+连线可用；变量默认值可能不完整（CDO 编译时序） |
| `L3_MINIMAL` | 最低可用读取 | 仅节点标题 + 类型，无连线 | 仅 basic_info + variables(名+类型) + graphs(节点标题+类型，无pins) |

---
## 二、数据类：BlueprintBasicInfo（蓝图基本信息）

| # | 字段 | 类型 | 必填 | 默认值 | 含义 | 示例值 |
|:--|:---|:---|:---|:---|:---|:---|
| 1 | `name` | `str` | ✅ | — | 蓝图名称（不含路径） | `BP_EnemyController` |
| 2 | `asset_path` | `str` | ✅ | — | 蓝图完整资产路径 | `/Game/Blueprints/Enemy/BP_EnemyController` |
| 3 | `parent_class` | `str` | ✅ | — | 父类名称 | `AIController` |
| 4 | `blueprint_type` | `str` | ❌ | `""` | 蓝图类型 | `"Blueprint"` / `"WidgetBlueprint"` / `"AnimBlueprint"` |
| 5 | `interfaces` | `List[str]` | ❌ | `[]` | 实现的接口列表 | `["IInteractable", "IDamageable"]` |
| 6 | `last_modified` | `str` | ❌ | `""` | 最后修改时间（ISO 格式） | `"2026-07-22T14:30:00"` |
| 7 | `node_count_total` | `int` | ❌ | `0` | 蓝图全部节点数 | `42` |
| 8 | `graph_count` | `int` | ❌ | `0` | 图（Graph）数量 | `3` |

### 降级影响

| 字段 | L1 | L2 | L3 |
|:---|:---:|:---:|:---:|
| `blueprint_type` | ✅ | ✅ | ✅ |
| `interfaces` | ✅ | ✅ | ⚠️ 可能为空 |
| `last_modified` | ✅ | ✅ | ⚠️ 可能为空 |
| `node_count_total` | ✅ | ✅ | ⚠️ 仅标题级计数 |
| `graph_count` | ✅ | ✅ | ✅ |

---
## 三、数据类：PinInfo（引脚信息）

| # | 字段 | 类型 | 必填 | 默认值 | 含义 | 示例值 |
|:--|:---|:---|:---|:---|:---|:---|
| 1 | `name` | `str` | ✅ | — | 引脚名称 | `"execute"` / `"then"` / `"ReturnValue"` |
| 2 | `direction` | `str` | ✅ | — | 引脚方向 | `"Input"` / `"Output"` |
| 3 | `pin_type` | `str` | ❌ | `""` | 引脚数据类型 | `"exec"` / `"object"` / `"int"` / `"float"` / `"bool"` / `"string"` |
| 4 | `is_exec` | `bool` | ❌ | `False` | 是否为执行流引脚 | `True`（exec/then 类引脚） |
| 5 | `is_connected` | `bool` | ❌ | `False` | 是否已有连线 | `True` |
| 6 | `pin_id` | `str` | ❌ | `""` | 引脚唯一标识符 | `"pin_A1B2C3"` |
| 7 | `default_value` | `str` | ❌ | `""` | 引脚默认值（字符串形式） | `"100.0"` / `"None"` / `"Actor'"` |
| 8 | `sub_category` | `str` | ❌ | `""` | 引脚子类别（如具体 Object 类型） | `"Actor"` / `"StaticMesh"` |

### 降级影响

| 字段 | L1 | L2 | L3 |
|:---|:---:|:---:|:---:|
| `pin_type` | ✅ | ✅ | ❌ |
| `is_connected` | ✅ | ✅ | ❌ |
| `pin_id` | ✅ | ✅ | ❌ |
| `default_value` | ✅ | ⚠️ 可能为空 | ❌ |
| `sub_category` | ✅ | ✅ | ❌ |

> **L3 说明：** 最低降级下 PinInfo 列表为空（`pins: []`），不产出引脚级别信息。

---
## 四、数据类：NodeInfo（节点信息）

| # | 字段 | 类型 | 必填 | 默认值 | 含义 | 示例值 |
|:--|:---|:---|:---|:---|:---|:---|
| 1 | `title` | `str` | ✅ | — | 节点标题（图中显示名） | `"BeginPlay"` / `"PrintString"` |
| 2 | `node_class` | `str` | ✅ | — | 节点类名（UE 内部类型） | `"K2Node_Event"` / `"K2Node_CallFunction"` / `"K2Node_IfThenElse"` |
| 3 | `pos_x` | `int` | ❌ | `0` | 节点在 Graph 中的 X 坐标 | `256` |
| 4 | `pos_y` | `int` | ❌ | `0` | 节点在 Graph 中的 Y 坐标 | `512` |
| 5 | `node_guid` | `str` | ❌ | `""` | 节点唯一标识符（GUID） | `"A1B2C3D4-E5F6-7890"` |
| 6 | `graph_name` | `str` | ❌ | `""` | 节点所属图名称 | `"EventGraph"` |
| 7 | `pins` | `List[PinInfo]` | ❌ | `[]` | 引脚列表（见 PinInfo） | `[]` 或含多个 PinInfo |
| 8 | `is_event` | `bool` | ❌ | `False` | 是否为事件节点 | `True`（如 BeginPlay、Tick） |
| 9 | `is_pure` | `bool` | ❌ | `False` | 是否为纯函数节点（无执行引脚） | `True`（如 GetActorLocation） |
| 10 | `is_latent` | `bool` | ❌ | `False` | 是否为延迟节点（跨帧执行） | `True`（如 Delay、Timeline） |
| 11 | `function_name` | `str` | ❌ | `""` | 函数名（仅 CallFunction 节点） | `"PrintString"` / `"SpawnActor"` |
| 12 | `target_class` | `str` | ❌ | `""` | 目标类（仅 CallFunction 节点） | `"KismetSystemLibrary"` / `"GameplayStatics"` |

### 常见 `node_class` 值

| node_class | 含义 |
|:---|:---|
| `K2Node_Event` | 事件节点（BeginPlay、Tick 等） |
| `K2Node_CallFunction` | 函数调用节点 |
| `K2Node_IfThenElse` | Branch 分支节点 |
| `K2Node_VariableGet` | 变量读取节点 |
| `K2Node_VariableSet` | 变量写入节点 |
| `K2Node_DynamicCast` | CastTo 类型转换节点 |
| `K2Node_SpawnActorFromClass` | 生成 Actor 节点 |
| `K2Node_ExecutionSequence` | Sequence 序列节点 |
| `K2Node_Timeline` | Timeline 时间轴节点 |

### 降级影响

| 字段 | L1 | L2 | L3 |
|:---|:---:|:---:|:---:|
| `pins` | ✅（完整引脚） | ✅（完整引脚） | ❌（空列表） |
| `pos_x / pos_y` | ✅ | ✅ | ❌（均为 0） |
| `node_guid` | ✅ | ✅ | ❌（空字符串） |
| `is_event / is_pure / is_latent` | ✅ | ✅ | ⚠️ 仅靠标题推断 |
| `function_name` | ✅ | ✅ | ⚠️ 仅靠标题推断 |
| `target_class` | ✅ | ✅ | ❌ |

---
## 五、数据类：ConnectionInfo（连线信息）

> ⚠️ **注意：** `from_node` / `from_pin` 为必填，但 `to_node` / `to_pin` 可空 —— 连线可能只识别出源端（如 L2 降级下部分连线不完整）。

| # | 字段 | 类型 | 必填 | 默认值 | 含义 | 示例值 |
|:--|:---|:---|:---|:---|:---|:---|
| 1 | `from_node` | `str` | ✅ | — | 连线来源节点标题 | `"BeginPlay"` |
| 2 | `from_pin` | `str` | ✅ | — | 连线来源引脚名称 | `"then"` |
| 3 | `from_direction` | `str` | ❌ | `""` | 来源引脚方向 | `"Output"` |
| 4 | `from_guid` | `str` | ❌ | `""` | 来源节点 GUID | `"A1B2C3D4"` |
| 5 | `to_node` | `str` | ❌ | `""` | 连线目标节点标题 | `"PrintString"` |
| 6 | `to_pin` | `str` | ❌ | `""` | 连线目标引脚名称 | `"execute"` |
| 7 | `to_direction` | `str` | ❌ | `""` | 目标引脚方向 | `"Input"` |
| 8 | `to_guid` | `str` | ❌ | `""` | 目标节点 GUID | `"E5F6G7H8"` |

### 降级影响

| 字段 | L1 | L2 | L3 |
|:---|:---:|:---:|:---:|
| 全部字段 | ✅ | ✅ | ❌ 完全不产出 |

> **L3 说明：** 最低降级下 `BlueprintAnalysis.connections` 和 `GraphAnalysis.connections` 均为空列表。无法读取连线拓扑。

---
## 六、数据类：GraphAnalysis（单图分析结果）

| # | 字段 | 类型 | 必填 | 默认值 | 含义 | 示例值 |
|:--|:---|:---|:---|:---|:---|:---|
| 1 | `graph_name` | `str` | ✅ | — | 图名称 | `"EventGraph"` / `"MyFunction"` |
| 2 | `graph_type` | `str` | ❌ | `""` | 图类型 | `"EventGraph"` / `"FunctionGraph"` / `"ConstructionScript"` |
| 3 | `node_count` | `int` | ❌ | `0` | 该图节点数量 | `15` |
| 4 | `nodes` | `List[NodeInfo]` | ❌ | `[]` | 节点列表（见 NodeInfo） | `[]` 或含多个 NodeInfo |
| 5 | `connections` | `List[ConnectionInfo]` | ❌ | `[]` | 连线列表（见 ConnectionInfo） | `[]` 或含多个 ConnectionInfo |

### `graph_type` 可选值

| 值 | 含义 |
|:---|:---|
| `EventGraph` | 事件图（蓝图主图，含 BeginPlay/Tick） |
| `FunctionGraph` | 函数图（自定义函数） |
| `ConstructionScript` | 构造脚本（Construction Script） |

### 降级影响

| 字段 | L1 | L2 | L3 |
|:---|:---:|:---:|:---:|
| `nodes`（含 pins） | ✅ | ✅ | ⚠️ 节点无 pins |
| `connections` | ✅ | ✅ | ❌（空列表） |
| `graph_type` | ✅ | ✅ | ✅ |

---
## 七、数据类：BlueprintVariable（蓝图变量）

| # | 字段 | 类型 | 必填 | 默认值 | 含义 | 示例值 |
|:--|:---|:---|:---|:---|:---|:---|
| 1 | `var_name` | `str` | ✅ | — | 变量名称 | `"Health"` |
| 2 | `var_type` | `str` | ✅ | — | 变量类型 | `"float"` / `"int32"` / `"bool"` / `"String"` / `"Actor"` |
| 3 | `default_value` | `str` | ❌ | `""` | 默认值（字符串形式） | `"100.0"` / `"None"` |
| 4 | `category` | `str` | ❌ | `"Default"` | 变量分类（Category 分组） | `"Combat"` / `"Movement"` |
| 5 | `is_exposed` | `bool` | ❌ | `True` | 是否暴露给蓝图实例面板 | `True`（勾选 Instance Editable） |
| 6 | `is_array` | `bool` | ❌ | `False` | 是否为数组类型 | `True`（如 `TArray<int32>`） |
| 7 | `is_editable` | `bool` | ❌ | `True` | 是否可在编辑器中修改 | `False`（如 `VisibleAnywhere`） |
| 8 | `tooltip` | `str` | ❌ | `""` | 变量提示文本（Tooltip） | `"角色当前生命值，0-100"` |

### 降级影响

| 字段 | L1 | L2 | L3 |
|:---|:---:|:---:|:---:|
| `default_value` | ✅ | ⚠️ CDO 时序可能导致为空 | ❌ |
| `category` | ✅ | ✅ | ✅ |
| `is_exposed / is_editable` | ✅ | ✅ | ⚠️ 可能均为默认值 |
| `tooltip` | ✅ | ✅ | ❌ |

---
## 八、数据类：AssetReference（引用资产）

| # | 字段 | 类型 | 必填 | 默认值 | 含义 | 示例值 |
|:--|:---|:---|:---|:---|:---|:---|
| 1 | `asset_path` | `str` | ✅ | — | 被引用的资产路径 | `"/Game/Blueprints/BP_DamageType"` |
| 2 | `asset_type` | `str` | ✅ | — | 引用方式 | `"CastTo"` / `"CreateWidget"` / `"SpawnActor"` / `"LoadObject"` |
| 3 | `source_node` | `str` | ❌ | `""` | 发起引用的节点标题 | `"CastTo_BP_DamageType"` |
| 4 | `is_suspicious` | `bool` | ❌ | `False` | 是否为可疑引用（路径不匹配/文件缺失） | `True` |
| 5 | `note` | `str` | ❌ | `""` | 可疑原因说明 | `"路径不匹配: 目标蓝图可能已被移动"` |

### `asset_type` 可选值

| 值 | 含义 | 检测来源 |
|:---|:---|:---|
| `CastTo` | CastTo 节点类型转换引用 | `K2Node_DynamicCast` |
| `CreateWidget` | 创建 Widget 引用 | `CreateWidget` 函数调用 |
| `SpawnActor` | 生成 Actor 引用 | `SpawnActorFromClass` 节点 |
| `LoadObject` | 资源加载引用 | 字符串路径参数 |

### 降级影响

| 字段 | L1 | L2 | L3 |
|:---|:---:|:---:|:---:|
| 全部字段 | ✅ | ✅ | ❌ 完全不产出 |

---
## 九、顶层结构：BlueprintAnalysis（完整分析结果）

| # | 字段 | 类型 | 必填 | 默认值 | 含义 |
|:--|:---|:---|:---|:---|:---|
| 1 | `basic_info` | `BlueprintBasicInfo` | ✅ | — | 蓝图基本信息（见 §二） |
| 2 | `variables` | `List[BlueprintVariable]` | ❌ | `[]` | 蓝图变量清单（见 §七） |
| 3 | `graphs` | `Dict[str, GraphAnalysis]` | ❌ | `{}` | 图字典，key=图名，value=单图分析（见 §六） |
| 4 | `connections` | `List[ConnectionInfo]` | ❌ | `[]` | 顶层连线汇总（见 §五） |
| 5 | `referenced_assets` | `List[AssetReference]` | ❌ | `[]` | 引用资产清单（见 §八） |
| 6 | `related_blueprints` | `List[str]` | ❌ | `[]` | 关联蓝图路径列表 |
| 7 | `degrade_level` | `DegradeLevel` | ❌ | `L1_NATIVE` | 本次使用的降级等级（见 §一） |
| 8 | `errors` | `List[str]` | ❌ | `[]` | 分析过程中的错误信息 |
| 9 | `warnings` | `List[str]` | ❌ | `[]` | 分析过程中的警告信息 |
| 10 | `analysis_time` | `str` | ❌ | `""` | 分析完成时间（ISO 格式） |

### 降级总览

| 字段 | L1_NATIVE | L2_BRIDGE | L3_MINIMAL |
|:---|:---:|:---:|:---:|
| `basic_info` | ✅ 完整 | ✅ 完整 | ⚠️ `interfaces`/`last_modified`/`node_count_total` 可能为空 |
| `variables` | ✅ 完整 | ⚠️ `default_value` 可能为空 | ⚠️ 仅 `var_name`+`var_type` |
| `graphs.*.nodes` | ✅ 含完整 pins | ✅ 含完整 pins | ⚠️ 仅 title+node_class，无 pins |
| `graphs.*.connections` | ✅ | ✅ | ❌ 空列表 |
| `connections` | ✅ | ✅ | ❌ 空列表 |
| `referenced_assets` | ✅ | ✅ | ❌ 空列表 |
| `related_blueprints` | ✅ | ✅ | ❌ 空列表 |

---
## 十、JSON 序列化格式

`BlueprintAnalysis.to_json()` 输出示例：

```json
{
  "basic_info": {
    "name": "BP_MyActor",
    "asset_path": "/Game/Blueprints/BP_MyActor",
    "parent_class": "Actor",
    "blueprint_type": "Blueprint",
    "interfaces": [],
    "last_modified": "2026-07-22T14:30:00",
    "node_count_total": 12,
    "graph_count": 1
  },
  "variables": [
    {
      "var_name": "Health",
      "var_type": "float",
      "default_value": "100.0",
      "category": "Combat",
      "is_exposed": true,
      "is_array": false,
      "is_editable": true,
      "tooltip": "角色当前生命值"
    }
  ],
  "graphs": {
    "EventGraph": {
      "graph_name": "EventGraph",
      "graph_type": "EventGraph",
      "node_count": 5,
      "nodes": [],
      "connections": []
    }
  },
  "connections": [],
  "referenced_assets": [],
  "related_blueprints": [],
  "degrade_level": "L1_NATIVE",
  "errors": [],
  "warnings": [],
  "analysis_time": "2026-07-24T10:00:00"
}
```

---
## 十一、字段类型速查

| Python 类型 | JSON 等效 | 说明 |
|:---|:---|:---|
| `str` | `string` | UTF-8 字符串 |
| `int` | `number` | 整数 |
| `bool` | `boolean` | 布尔值 |
| `List[T]` | `array` | 列表，元素类型为 T |
| `Dict[str, T]` | `object` | 字典，key 为字符串，value 为 T |
| `DegradeLevel` | `string` | 枚举序列化为 `.name`（如 `"L1_NATIVE"`） |

---

> **维护者：** dev（代码开发 Agent）  
> **同步规则：** 数据类字段变更时，同步更新本文档。
