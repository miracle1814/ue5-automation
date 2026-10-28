---
slug: ue5-automation
name: ue5-automation
displayName: UE5 蓝图自动化
version: 6.5.4
summary: 让 AI 直接读写 UE5 蓝图节点、做编译诊断与 PIE 行为验证；Windows + UE 5.1/5.8。
description: UE5 蓝图自动化操作规范：先读后写、整图事务、读回验证、编译诊断、运行时不宕机断言、任务级撤销与多 Agent 协作。适用于已经安装 ue5-automation bridge/MCP 的 UE5 工程。
license: Apache-2.0
homepage: https://github.com/miracle1814/ue5-automation
tags:
  - unreal-engine
  - blueprint
  - mcp
  - game-development
  - windows
  - automation
---

# UE5 蓝图自动化

本 Skill 是 `ue5-automation` 的操作契约。它不替代运行包：真正写 UE 编辑器的是
`ue5-automation` bridge（UE 插件 + 本机 HTTP/MCP 服务）。本 Skill 负责让 Agent：

- 在动手前先读清资产和图；
- 用整图事务写入，而不是一个节点一个节点地试；
- 写完后编译、读回、运行时验证；
- 失败时诚实报错，不把“命令返回 200”当成“蓝图已改对”；
- 多 Agent 并发时用幂等键/资产锁避免互相踩踏。

## 5 分钟最小成功路径

如果只想先确认“这东西真能用”，按下面跑一遍：

1. `health` → 确认 `engine_version` / `bridge_loaded`。
2. `build_blueprint{bp_name:"BP_Smoke", pkg:"/Game/Test/", parent:"Actor"}`
   → 建一个空蓝图，确认 `success=true`。
3. `search_blueprint_nodes{bp_path:"/Game/Test/BP_Smoke", query:"Print"}`
   → 确认能返回候选节点。
4. `apply_graph` 写入 `BeginPlay → PrintString`：
   - 函数节点带 `class:"/Script/Engine.KismetSystemLibrary"`；
   - 图级 `pin_defaults:{"print.InString":"Hello"}`。
5. `compile_and_diagnose` → 确认 `compiled=true`。
6. `get_pin_default_value{node_id:"PrintString", pin_name:"InString"}`
   → 读回 `Hello`。
7. `save_asset` → 确认文件落盘。

这条路径覆盖了“建资产 → 查节点 → 整图写入 → 编译 → 读回 → 保存”的核心闭环。

## 何时使用

当用户提出以下需求时使用：

- “帮我读一下这个蓝图 / 变量 / 节点 / 连线”
- “给这个蓝图加一段逻辑 / 连一条线 / 改默认值”
- “从零建一个蓝图 / 加组件 / 加接口 / 加事件”
- “编译报错了，帮我定位是哪个节点”
- “跑一下 PIE，确认这个属性/日志/画面真的对”
- “撤回刚才的改动 / 这个任务改了哪些资产”

触发词：UE5、Unreal、蓝图、Blueprint、蓝图节点、PIE、UMG、Material、DataTable、
Gameplay、蓝图自动化、UE 编辑器。

## 前置条件

| 项 | 要求 |
|---|---|
| 系统 | Windows（bridge 是 Win64 插件） |
| 引擎 | UE 5.1.x 开箱可用；5.8.x 有预编译 DLL；5.2–5.7 需按源码重编译 |
| 编辑器 | 启用 `Python Editor Script Plugin` 与 `Editor Scripting Utilities` |
| 运行包 | 已安装 `ue5-automation` 插件、宿主脚本与 bridge |
| MCP | 已连接本机 `http://127.0.0.1:8889/mcp`，或已配置 stdio 代理 |
| 权限 | 用户明确授权写入当前 UE 工程 |

如果 `health` / `ue5_check_health` 不可用，**不要假装工具已连接**；先让用户按
`references/INSTALL-WINDOWS.md` 安装运行包并启动编辑器。

## 第一条规则：先检查工具可用性

在写任何资产之前，先调用：

```json
{"tool":"ue5_check_health","arguments":{}}
```

或：

```json
{"command":"health","params":{}}
```

预期返回包含 `engine_version`、`project_name`、`bridge_loaded`。如果失败：

1. 明确告诉用户“bridge 未连接/插件未加载”；
2. 不要继续写操作；
3. 按 `references/INSTALL-WINDOWS.md` 排查。

## 标准工作流

### 1. 读：先看清再动手

```json
{"tool":"ue5_read_blueprint",
 "arguments":{"bp_path":"/Game/Blueprints/BP_Door","level":"L2"}}
```

- L1：类信息、变量、CDO；
- L2：节点与连线；
- `fields` 可裁剪返回字段，避免大图把上下文撑爆。

写图前还要查可调用节点和引脚签名：

```json
{"tool":"ue5_search_blueprint_nodes",
 "arguments":{"bp_path":"/Game/Blueprints/BP_Door","query":"Open","limit":20}}
```

### 2. 写：整图事务优先

主路径是 `apply_graph` / `ue5_apply_blueprint_graph`：

```json
{"tool":"ue5_apply_blueprint_graph",
 "arguments":{
   "bp_path":"/Game/Blueprints/BP_Door",
   "replace":true,
   "graph_spec":{
     "graph":"EventGraph",
     "nodes":[
       {"id":"begin","kind":"event","target":"ReceiveBeginPlay"},
       {"id":"print","kind":"function",
        "class":"/Script/Engine.KismetSystemLibrary",
        "target":"PrintString"}
     ],
     "edges":[{"from":"begin.then","to":"print.execute"}],
     "pin_defaults":{"print.InString":"Door opened"}
   }
 }}
```

> `pin_defaults` 是**图级**字段，键为 `"<节点id>.<引脚名>"`；写在单个节点里不会生效。
> 函数节点必须带完整类路径（如 `/Script/Engine.KismetSystemLibrary`），
> 只写 `KismetSystemLibrary` 在 5.8 上会定位失败。

约定：

- 节点引用优先用 32 位十六进制 GUID；用标题时不能有歧义；
- `replace=true` 才会替换已有图，空图首次写入也要显式确认；
- 返回值必须包含 `verified=true`；失败会回滚并返回错误，不要重试同一份错误 spec；
- 小手术才用 `connect_pins` / `disconnect_pin` / `delete_node`，不要用它们拼整张图。

### 3. 编译：看结构化诊断

```json
{"tool":"ue5_compile_and_diagnose",
 "arguments":{"bp_path":"/Game/Blueprints/BP_Door"}}
```

每个错误尽量带：资产、函数/图、节点标题、`node_guid`、引脚提示、规则库
`pitfall_ref/fix_hint`。下次修改用 `node_guid` 定位，不要靠标题猜。

### 4. 验证：编译通过不等于行为正确

运行时属性断言：

```json
{"tool":"ue5_verify_behavior",
 "arguments":{"actor_name":"BP_Door_1","property_name":"state",
              "expected":"Open","world":"pie","match":"exact"}}
```

多步场景（wait / assert / log_contains / screenshot / command）：

```json
{"tool":"ue5_verify_scenario",
 "arguments":{
   "name":"door_opens",
   "start_pie":true,
   "pie_mode":"simulate",
   "steps":[
     {"type":"wait","seconds":0.5},
     {"type":"assert","actor_name":"BP_Door_1","property_name":"state",
      "expected":"Open"},
     {"type":"log_contains","pattern":"Door opened","timeout":5},
     {"type":"screenshot","out_path":"door_scenario.png"}
   ]
 }}
```

- `verified=true` 才是通过；截图失败只算证据失败，不翻转 `verified`；
- 场景默认禁止 `delete_asset`、`agent_undo`、`batch_asset_ops`、`apply_graph{replace}`
  等高危命令；需要时显式 `allow_destructive=true`；
- 总超时默认 300s，上限 1800s。

### 5. 收尾：保存 + 任务账本

```json
{"command":"task_begin","params":{"goal":"给门加 BeginPlay 打印"}}
{"command":"task_end","params":{"status":"success","summary":"..."}}
```

- 写命令会自动留一个可撤销的 UE 事务步；
- `agent_undo_status` 看可回滚步数，`agent_undo` 回滚最近 N 个 agent 步；
- 回滚遇到用户操作会立即停，不会跳过用户改动。

## 常用命令速查

| 需求 | 命令 / 工具 |
|---|---|
| 读蓝图 | `read_blueprint` / `ue5_read_blueprint` |
| 查节点签名 | `search_blueprint_nodes` |
| 整图写入 | `apply_graph` / `ue5_apply_blueprint_graph` |
| 连线 | `connect_pins` / `ue5_connect_pins` |
| 断线 | `disconnect_pins` / `ue5_disconnect_pin` |
| 删节点 | `delete_node` / `ue5_delete_node` |
| 编译诊断 | `compile_and_diagnose` |
| 行为断言 | `verify_behavior` |
| 场景验收 | `verify_scenario` |
| 截图 | `capture_viewport` / `capture_pie` / `ue5_screenshot` |
| 资产治理 | `batch_asset_ops`（默认 dry-run） |
| 材质/UMG/DataTable | `material_*` / `widget_*` / `datatable_*` |
| 撤销 | `agent_undo_status` / `agent_undo` |
| 多 Agent 锁 | `session_lock_acquire/release/status` |
| 幂等重试 | 写命令加 `idempotency_key` |
| 最近回执 | `receipt_list` |

完整 101 条命令用：

```json
{"tool":"ue5_tool_search","arguments":{"query":"material","limit":10}}
```

## 多 Agent 协作

两个 Agent 同时写同一个蓝图时：

1. 写命令带 `session_id`（会话标识）；
2. 先 `session_lock_acquire{owner, assets:[...], ttl:600}`；
3. 冲突会返回 `asset_locked` / `session_lock`，不要重试同一资产；
4. 写完 `session_lock_release`，或等 TTL 过期；
5. 网络重试带 `idempotency_key`：同 key + 同命令/参数会重放上次成功结果，
   不会重复写；同 key 换命令会 `idempotency_conflict`。

锁是协作式，不是编辑器强制锁。所有写操作走 `execute_command` 才能经过锁层。

## 安全与权限

- bridge 只监听 `127.0.0.1`，需要 `X-Skill-Token`；
- 没有默认 token；token 存在本机 `%APPDATA%\ue5_bridge\token`；
- 命令白名单，不允许任意代码执行；
- 资产路径只允许 `/Game/`；
- 删除/批量操作默认 dry-run 或需要 `confirm`；
- 不向外部服务上传工程内容；日志读取有允许根；
- 写操作必须由用户明确授权；不要自动保存整个工程。

## 失败时怎么做

- **不要**把 `success=true` 当成“用户的目标已完成”；看 `verified`。
- 编译失败：先 `compile_and_diagnose`，按 `node_guid` 定位，再改。
- 写图失败：读回 `verified=false` 时不要继续叠加修改；先回滚/修正 spec。
- 工具不可用：明确说“MCP/bridge 未连接”，不要编造结果。
- 资产不存在：`asset_load` / `asset_not_found` 是明确失败，不要创建同名替代品
  除非用户要求。

## 边界

- Windows only；UE 5.2–5.7 需自行重编译 bridge；
- AnimBP 只读：能枚举图，节点级属性在部分版本未暴露；写入通道未开放；
- 场景真机验证覆盖 5.1/5.8 的 command/wait/断言；复杂 PIE 场景请先在工程内小步试；
- 无 TLS；跨机使用需自建隧道。

## 企业合作 / Commercial

- **企业集成**：把 UE 蓝图自动化接进团队现有 Agent / CI / 资产管线。
- **版本适配**：UE 5.2–5.7 的 bridge 重编译与回归。
- **定制开发**：领域模板、批量迁移、审计/审批、私有工具链。
- **培训/支持**：团队上手、故障排查、交付验收。
- 联系方式：GitHub Issue 标题注明 `[Commercial]`，或通过
  https://github.com/miracle1814 联系作者。

## 授权与商标

- 本项目原创代码：Apache-2.0；第三方组件与完整声明见仓库
  `THIRD_PARTY_NOTICES.md`。
- 截图/PIE 部分实现参考 UEBox / UnrealAgentLink（MIT），完整 MIT 文本随仓库与
  移植包分发。
- Unreal、Unreal Engine、UE 是 Epic Games, Inc. 的商标；本 Skill 与 Epic Games
  无隶属或背书关系。引擎头文件/二进制仍受 Epic EULA 约束。
- 本 Skill 不包含 Epic 美术资源、Logo、示例资产或市场内容。

## 参考

- 安装：`references/INSTALL-WINDOWS.md`
- 命令目录：`references/COMMANDS.md`
- 常见故障：`references/TROUBLESHOOTING.md`
- 验证记录：`references/VERIFICATION.md`
- 安全模型：`references/SECURITY.md`
- 项目主页：https://github.com/miracle1814/ue5-automation
