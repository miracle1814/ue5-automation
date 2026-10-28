# UE5 自动化 Skill · API 参考文档

> 自动生成 | 版本 v0.15 | 源文件: skills/ue5-automation/scripts/
> v0.13（2026-09-17 · T-20260917-RELEASE-V218）：新增「入参补充 · D-1~D-4」章节 —— `unpin_console_refs` 的 `asset_path` 语义与空参行为 / `set_pin_default_value`·`get_pin_default_value` 的 **GUID 定位**（`node_id` 或 `node_guid`）/ `add_function_node` 建节点失败 **fail-loud**；并记录 `/build` 事件存在性判定按语义键修正。
> v0.8（2026-09-12 · T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE-R4）：补 `ue5_bridge` `/command` 白名单 **L2 批次 21 条**命令条目（C++ API #31/#33-#59 的 Python 封装），口径 = 在线实测唯一基线 run_4（`71 passed + 1 xfailed / 0 failed`）。
> v0.7（2026-09-09 · T-20260909-SKILL-GEN-C-INIT-CONVERGE-REMAIN）：新增 `init.py` CLI/产物契约条目（薄执行器 + 预加载建议稿 + 人工确认门语义）

## 目录

- [🚀 新机器初始化（init.py）](#init.py)
- [🏗️ 蓝图构建（孤儿模块 · 见节内状态注）](#blueprint_builder)
- [🔍 蓝图读取](#blueprint_reader)
- [📊 项目分析](#analyzer)
- [📝 日志诊断](#log_parser)
- [🏃 UE 执行器](#ue5_runner)
- [🌉 HTTP Bridge](#ue5_bridge)
  - [L2 命令白名单（21 条）](#ue5_bridge)
- [🛡️ 飞前检查](#preflight)

---
## 🚀 新机器初始化（init.py）

**源文件:** `init.py`（v1.1 · 薄执行器 · T-20260909-SKILL-GEN-C-INIT-CONVERGE 语义收敛）

### CLI 契约

```bash
python scripts/init.py                      # dry-run 只读预览（默认，不落盘）
python scripts/init.py --apply              # 落盘建议稿（output/role_assignments.yaml, status=proposed）
python scripts/init.py --apply --assign <agent_id>=<role>   # 显式指派核心角色 → confirmed
python scripts/init.py --reconcile --apply  # 读回手编 role_assignments.yaml 合并后落盘 → confirmed
python scripts/init.py --deploy-script <path>  # 显式指定 deploy.py（默认三级定位）
python scripts/init.py --api-base <url>     # 覆盖 agent API 地址（默认 localhost:8088）
```

### 产物契约

| 产物 | 内容 | status |
|:---|:---|:---|
| `output/role_assignments.yaml` | 角色分配（orchestrator / ue_executor / screenshot_ocr / report_writer / archivist） | proposed（建议稿）→ confirmed（人工确认后生效） |
| `output/init_report.md` | health 自检 + 引擎/项目定位 | — |

### 角色判定规则

- single_agent（≤1 enabled）：宿主 `all_in_one`，role_source=`deterministic` → 自动 `confirmed`
- multi_agent 自动分配：role_source=`auto` → `proposed`，须人工确认门（`--assign` / `--reconcile --apply`）升级 manual → `confirmed`
- 防静默覆盖 GATE：既有 confirmed + 无人工通道 + role_map 变化 → 拒绝（exit 3）

### 内部模块函数（供 fixture/二次开发）

| 函数 | 说明 |
|:---|:---|
| `detect_agents()` | 能力探测三级链（L1 API → L2 agent.json → L3 system_prompt），返回 `(meta, records)`；≤1 enabled → `meta["mode"]="single_agent"` |
| `assign_roles(records, manual_map, mode)` | 角色分配，返回 `(role_map, records)`；single_agent 全 `all_in_one` deterministic |
| `_decide_status(mode, role_sources, manual_channels)` | 状态机：single_agent → `STATUS_CONFIRMED`；auto 残留 → `STATUS_PROPOSED` |

---
## 🏗️ 蓝图构建

**源文件:** `blueprint_builder.py`

> ⚠️ **模块状态（T-20260909-SKILL-DEFECT-FIX 补注）**：v0.6 起已被 `ue5_bridge.py`
> 的 GameThread 构建路径取代——**真实蓝图构建不经过本模块**，当前仅保留为 Python
> API 入口（孤儿模块）。生产用法请走 `ue5_runner.py build()` → `POST /build`。

### `class BlueprintType(Enum)`

```
(无文档字符串)
```

### `class BuildStep`

```
(无文档字符串)
```

### `class BuildResult`

```
(无文档字符串)
```

---
## 🔍 蓝图读取

**源文件:** `blueprint_reader.py`

### `class DegradeLevel(Enum)`

```
降级等级
```

### `class PinInfo`

```
引脚信息
```

### `class NodeInfo`

```
节点信息
```

### `class BlueprintAnalysis`

```
蓝图完整分析结果
```

### `class BlueprintReader`

```
蓝图逆向识别引擎。

用法：
reader = BlueprintReader()
analysis = reader.analyze("/Game/Blueprints/BP_HelloMirror")
print(analysis.to_json())

运行模式：
- remote：通过 ue_send.py 向 UE 发送探针代码（默认）
- embedded：在 UE Python 控制台中直接运行
```

---
## 📊 项目分析

**源文件:** `analyzer.py`

### `class BlueprintSummary`

```
单张蓝图摘要。
```

### `class CircularDependency`

```
循环依赖。
```

### `class CouplingMetrics`

```
耦合度指标。
```

### `class DistributionStats`

```
类型分布统计。
```

### `class AnalysisResult`

```
完整分析结果。
```

### `class ComplexityRating(Enum)`

```
复杂度评级
```

### `class DeepAnalysisReport`

```
深度分析报告
```

### `class ProjectAnalyzer`

```
项目架构分析引擎。

两种运行模式：
1. 离线分析 — 传入 blueprint_reader 的数据源（BlueprintReadResult 列表）
2. UE 环境直连 — 运行时通过 unreal + bridge 扫描

使用方式：
analyzer = ProjectAnalyzer()
analyzer.feed(blueprint_results)          # 喂入 reader 数据
result = analyzer.analyze(project_root)   # 执行分析
```

### `ProjectAnalyzer.feed_from_json(json_path)`

```
从 JSON 文件加载 reader 输出（项目级 → self.feed 列表/单蓝图）。

Args:
json_path: blueprint_reader.py 输出的 JSON 文件路径

Returns:
无（直接更新 self.bp_data / self.bp_dep 等状态）

随后调 analyzer.analyze(project_root) → AnalysisResult
```

### `analyze_from_reader_output(reader_output_path, project_root="/Game/", depth=2)`

```
便捷函数：从 blueprint_reader JSON 输出直接分析。

Args:
- reader_output_path: blueprint_reader.py 输出的 JSON 文件路径
- project_root: 项目根目录，默认 "/Game/"
- depth: 依赖分析深度，默认 2

Returns:
- AnalysisResult（ProjectAnalyzer.analyze 返回值）

实现：内部 = ProjectAnalyzer() + feed_from_json + analyze
```

### `generate_markdown_report(report)`

```
将 DeepAnalysisReport 生成为 Markdown 报告。

Args:
report: 深度分析报告

Returns:
Markdown 格式的字符串
```

### `deep_analyze(blueprint_analysis, output_dir=None)`

```
一键深度分析 + 生成 JSON/MD 双格式报告。

Args:
blueprint_analysis: 来自 blueprint_reader 的 BlueprintAnalysis
output_dir: 输出目录，默认输出到蓝图自动化 output/

Returns:
{"json": path, "md": path, "report": DeepAnalysisReport}
```

---
## 📝 日志诊断

**源文件:** `log_parser.py`

### `class AutoFixExecutor`

```
(无文档字符串)
```

### `parse_log_line(line, line_num=0)`

```
(无文档字符串)
```

---
## 🏃 UE 执行器

**源文件:** `ue5_runner.py`

### `class UE5Runner`

```
UE5 混合执行器：读用 ue_send，写用 HTTP bridge。
```

---
## 🌉 HTTP Bridge

**源文件:** `ue5_bridge.py`

### `start(port=None, host='127.0.0.1')`

```
启动 Bridge 服务器（在 UE Python Console 中调用）。

🔒 B-003 修复：默认端口从环境变量 UE5_BRIDGE_PORT 读取，回退 8889。

用法：
import ue5_bridge
ue5_bridge.start()

Args:
port: HTTP 监听端口，默认从环境变量 UE5_BRIDGE_PORT 读取（回退 8889）
host: 监听地址，默认 127.0.0.1
```

### `stop()`

```
停止 Bridge 服务器。
```

### L2 命令白名单（21 条 · `/command` · 在线实测）

**源文件:** `ue5_bridge.py`（`_COMMAND_WHITELIST` · L2 批次 21 条 · T-20260912-UE5BRIDGE-PYWRAP-L2-IMPL 实装 · T-20260912-...-L2-ONLINE 在线联调）

**入口:** `POST /command` · 头 `X-Skill-Token` · 体 `{"command": "<命令>", "params": {...}, "timeout": 60.0}`

**白名单总量:** **72 条** (v3.0 融合升级) = 15 基础 + 11 L1 + 21 L2 + 1 落盘 + 2 资产（`delete_asset` / `unpin_console_refs` · 2026-09-17 `ast` 实测订正，原记 48 条）；未登记命令 → `success=false` + `Unknown command`（fail-loud；不提供任意代码执行入口）。

| # | `command` | 入参（签名） | 对应 C++ API | 在线结论（run_4） |
|:--:|:---|:---|:---|:---|
| 1 | `add_cast_node` | `(bp_path, graph_name=EventGraph, target_class_name, node_pos)` | AddCastNode(#33) | ✅ — `title="类型转换为 Actor"` · 608ms |
| 2 | `add_class_cast_node` | `(bp_path, graph_name=EventGraph, target_class_name, node_pos)` | AddClassCastNode(#34) | ✅ — `title="类型转换为 Actor类"` · 594ms |
| 3 | `add_macro_node` | `(bp_path, graph_name=EventGraph, macro_name, node_pos)` | AddMacroNode(#35) | ✅ — `title="For Each Loop"` · 619ms；**24 候选宏名全扫 `ok=24 / fail=0`** |
| 4 | `add_switch_node` | `(bp_path, graph_name=EventGraph, type_kind, enum_or_type_name, node_pos)` | AddSwitchNode(#36) | ✅ — `int` → `切换整型`；`enum` → `切换ECollisionChannel` / `切换EInputEvent` |
| 5 | `add_struct_node` | `(bp_path, graph_name=EventGraph, struct_name, b_is_break, node_pos)` | AddStructNode(#37) | ✅ — `make` → `"创建Vector"`；`break` → `"中断Vector"` |
| 6 | `add_enum_literal_node` | `(bp_path, graph_name=EventGraph, enum_name, value_name, node_pos)` | AddEnumLiteralNode(#56) | ✅ — `title="文字枚举ECollisionChannel"` ×2 |
| 7 | `add_composite_node` | `(bp_path, graph_name=EventGraph, node_pos)` | AddCompositeNode(#54) | ✅ — `title="EdGraph_0\n合并的图表"`（`pins` 回读为空 → 已留 warning，不静默） |
| 8 | `add_math_expression_node` | `(bp_path, graph_name=EventGraph, expression, node_pos)` | AddMathExpressionNode(#38) | ✅ — `title="(A + B) * C\n数学表达式"` · 653ms |
| 9 | `add_async_action_node` | `(bp_path, graph_name=EventGraph, factory_function_name, factory_class_name, activate_function_name, node_pos)` | AddAsyncActionNode(#55) | ✅ — `title="延迟"` · 628ms（ProxyFactory 反射写入） |
| 10 | `add_create_delegate_node` | `(bp_path, graph_name=EventGraph, function_name, node_pos)` | AddCreateDelegateNode(#45) | ✅ — `title="创建事件"` · 590ms |
| 11 | `add_actor_bound_event_node` | `(bp_path, graph_name=EventGraph, actor_name, delegate_name, node_pos)` | AddActorBoundEventNode(#43) | ✅ — `title="Actor开始重叠时 (StaticMeshActor0)"` |
| 12 | `add_input_axis_event_node` | `(bp_path, graph_name=EventGraph, axis_name, b_is_key_axis, node_pos)` | AddInputAxisEventNode(#41) | ✅ — 按键轴 `"W"`；命名轴 `"输入轴Move Forward / Backward"` |
| 13 | `add_input_action_event_node` | `(bp_path, graph_name=EventGraph, action_name, node_pos)` | AddInputActionEventNode(#40) | ✅ — `title="输入操作Jump"` |
| 14 | `add_component_bound_event_node` | `(bp_path, graph_name=EventGraph, component_name, delegate_name, node_pos)` | AddComponentBoundEventNode(#42) | ✅ — `title="组件开始重叠时 (PywrapBox)"` |
| 15 | `add_pin_to_node` | `(bp_path, graph_name=EventGraph, node_guid, direction, pin_type, pin_name)` | AddPinToNode(#47) | ✅ — `ok=true` |
| 16 | `remove_pin_from_node` | `(bp_path, graph_name=EventGraph, node_guid, pin_name)` | RemovePinFromNode(#48) | ✅ — `ok=true · removed=true` |
| 17 | `try_node_add_input_pin` | `(bp_path, graph_name=EventGraph, node_guid, strict)` | TryNodeAddInputPin(#49) | ⚠️ xfail — ⚠️ **XFAIL**（引擎层不支持：UE5.1 `K2Node_SwitchInteger` 未实现 `IK2Node_AddPinInterface`） |
| 18 | `set_node_property` | `(bp_path, graph_name=EventGraph, node_guid, property_name, value)` | SetNodeProperty(#31) | ✅ — `NodeComment` / `bCanRenameNode` 均 `ok=true` |
| 19 | `remove_component` | `(bp_path, comp_name)` | RemoveComponent(#58) | ✅ — `ok=true`（`removed` 回读不可用 → `warnings[2]`，不静默） |
| 20 | `add_implemented_interface` | `(bp_path, interface_class_path)` | AddImplementedInterface(#59) | ✅（含 R3 硬化） — 加固前实测 `ok=true` 330ms（D-1 族入口） |
| 21 | `add_event_dispatcher` | `(bp_path, delegate_name)` | AddEventDispatcher(#46) | ✅ — `ok=true` |

**负路径总览（fail-loud 必测）:** 同名引脚 / 同名事件分发器 / 不存在组件 / 重复接口 / 不命中 `node_guid` / 不存在 graph / 蓝图不存在 / 非法入参 → 全部 `success=false` + `BridgeError: …`（8 类，原文见报告 §2）。

**M-2 类型铁律（调用方必读）:** `read_nodes` / `list_nodes` 返回 `FBPNodeInfo`（USTRUCT，**不是**节点）→ 需真实节点时必须 `get_node_by_guid` 换回 `UEdGraphNode*`；`get_pin_infos` 只吃 `BPNodeInfo`。

**相关:** `docs/L2-COMMANDS.md`（逐条详表）· `SKILL.md` §2.3（命令 × C++ API 对照）· `workflow.md` 附录 B（调度速查）。

#### ⚠️ 入参补充（T-20260916-UE5AUTO-HALLBEACON-D1D4-FIX · 三处修正 · 2026-09-16）

| 命令 | 补充说明 |
|:---|:---|
| `unpin_console_refs` | **入参 `asset_path`（目标资产路径 · 必填语义）**：`{"command":"unpin_console_refs","params":{"asset_path":"/Game/Blueprints/BP_X"}}`（亦接受 `/Game/X/Y.BP_Y` / `/Game/X/Y:BP_Y`，内部按 `.` / `:` 截断取包名）。语义 = 「**只**解绑 `globals()` 中 path 指向**该资产**的 UObject 包装器 + `collect_garbage()`」；⛔ **空参 / 无参不执行任何删除**（返回 `success=false` + `error`）——本命令**不提供全量清理语义**。修正前白名单绑定为 `lambda asset_path, **kw`（无默认值）→ 传 `params: {}` 抛裸 `TypeError: missing 1 required positional argument: 'asset_path'`（P5 实测），现改为默认 `""` + 可读错误。 |
| `set_pin_default_value` / `get_pin_default_value` | `node_id` 现**同时接受节点标题与 32 位 GUID**（GUID 形如 `_pywrap_is_guid_like` 判定 → 优先走 `GetNodeByGuid` 精确命中，同名节点场景唯一可靠锚点；亦可用显式别名 `node_guid`）。**标题定位保持向后兼容**。返回体新增 `matched_by` ∈ {`guid`, `title`}。 |
| `add_function_node` | 建节点失败（DLL 返回 None）→ **必抛 `BridgeError`** 并附可读原因 + **函数名候选**（原名 / 去 `K2_` / 加 `K2_`），不再返回 `{"added_node": …}` 的**假阳性成功**。（P5 实测：`/Script/Engine.Actor:K2_SetActorHiddenInGame` → AActor 真实函数名为 `SetActorHiddenInGame`，`K2_` 前缀仅用于重载冲突函数。） |

> `/build` 事件存在性判定（`game_thread_build.py.tmpl`）同步修正：本地化标题候选**只保留与 `event_name` 语义同键**的项（`_norm_event_key` 归一化匹配），候选表补齐 `Begin/EndOverlap` 等缺口，`exists` 步回传 `matched_by` / `matched_title` / `candidates` —— 杜绝「用无关事件的本地化标题误判 exists → **静默漏建**」。
---
## 🛡️ 飞前检查

**源文件:** `preflight.py`

### `run_batch(manifest)`

```
(无文档字符串)
```

---

*本文档由 `_extract_api_docs.py` 自动生成。*