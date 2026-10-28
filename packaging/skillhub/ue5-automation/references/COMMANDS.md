# 命令目录（按任务分组）

完整 101 条命令以运行包 `tool_search` 为准；这里是 Agent 最常用的入口。

## 读

| 命令 | 用途 |
|---|---|
| `health` | 引擎/项目/bridge 状态 |
| `list_assets` | 列资产 |
| `read_blueprint` | L1/L2/full 读蓝图 |
| `read_variables` | 变量 |
| `read_cdo` | 类默认对象 |
| `read_nodes` | 节点 |
| `read_connections` | 连线 |
| `search_blueprint_nodes` | 按关键词查可调用节点+引脚签名 |
| `get_pin_default_value` | 读引脚默认值 |
| `get_node_by_guid` | 按 GUID 定位节点 |
| `find_actors` / `get_actor_properties` | 关卡 Actor |
| `logs_read` | 游标增量日志 |
| `datatable_read` | DataTable 行 |

## 写蓝图

| 命令 | 用途 |
|---|---|
| `build_blueprint` | 原子建资产（失败回滚） |
| `apply_graph` | 整图声明式写入（主路径） |
| `add_variable` | 加变量 |
| `add_component` | 加组件 |
| `add_function_node` | 加函数调用节点 |
| `connect_pins` | 连线（拓扑验证） |
| `disconnect_pins` | 断线 |
| `delete_node` | 删节点 |
| `set_pin_default_value` | 改默认值 |
| `set_node_position` | 移动节点 |
| `add_cast_node` / `add_macro_node` / `add_switch_node` | 常用节点 |
| `save_asset` | 保存 |
| `delete_asset` | 删除（高危，需确认） |

## 编译/验证

| 命令 | 用途 |
|---|---|
| `compile_blueprint` | 编译 + 回读状态 |
| `compile_and_diagnose` | 结构化错误（node_guid） |
| `pie_start` / `pie_stop` / `pie_state` | PIE 生命周期 |
| `verify_behavior` | 运行时属性断言 |
| `verify_scenario` | 多步场景 + JUnit |
| `capture_viewport` / `capture_pie` | 截图证据 |

## 资产治理

| 命令 | 用途 |
|---|---|
| `batch_asset_ops` | 批量重命名/搬迁，默认 dry-run |
| `task_assets` | 任务资产账本 |
| `rollback_task_assets` | 回滚任务创建/删除的资产 |

## 任务/撤销

| 命令 | 用途 |
|---|---|
| `task_begin` / `task_step` / `task_end` | 任务账本 |
| `agent_undo_status` | 可回滚步数 |
| `agent_undo` | 回滚最近 N 个 agent 步 |

## 多 Agent

| 命令 | 用途 |
|---|---|
| `session_lock_acquire` | 抢显式资产锁 |
| `session_lock_release` | 释放 |
| `session_lock_status` | 查看 |
| `receipt_list` | 最近写回执 |
| `idempotency_key`（参数） | 重试不重复写 |

## 其他

材质：`material_create` / `material_add_expression` /
`material_connect_*` / `material_compile` / `material_read` /
`material_instance_*`

UMG：`widget_create` / `widget_add_child` / `widget_set_property` /
`widget_read_tree`

DataTable：`datatable_read` / `datatable_write_csv` / `datatable_create`

批量：`editor_batch`（白名单命令序列）

只读探针：`class_probe` / `debug_reflect` / `animbp_probe` / `animbp_read`

> 不确定命令名时先 `tool_search{query:"关键词"}`，不要猜参数。
