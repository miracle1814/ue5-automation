# v4.5 · 任务级撤销（Task Undo）实施与验证说明

> 日期：2026-10-27 · 对应计划：`PLAN-AUDIT-v4.4.md` 的 **C 项**
> 结论：**已完成**。5.1 / 5.8 双引擎真机通过，离线 28 项契约测试通过。

---

## 1. 要解决的问题

agent 通过 Python 改蓝图时，改动通常不在 UE 事务里：

- 编辑器 `Ctrl+Z` 看不到 agent 的动作；
- 用户想"这整个任务不要了"时，只能让 agent 反向操作，或者手动删节点；
- apply_graph 的失败回滚只覆盖"本次新建的节点"，管不了属性修改与旧节点删除。

目标：**一条命令回滚 agent 刚做的 N 步**，且绝不误撤用户自己的操作。

## 2. 设计（最小侵入，不引入独立撤销栈）

| 决策 | 做法 | 原因 |
|---|---|---|
| 用哪个事务栈 | **UE 原生** `GEditor->Trans` | 与编辑器撤销/重做完全一致；用户 Ctrl+Z 也能看到同样的一步 |
| 怎么区分 agent 步 | 事务标题统一前缀 `[agent] ` | 唯一的安全边界：回滚逐条校验标题，不靠"信任调用方" |
| 谁开事务 | 写命令**自动**各开一个（`_dispatch_with_tx`） | agent 不需要记 begin/end；一个命令 = 一个撤销步 |
| 嵌套怎么办 | C++ 深度计数，只在最外层真正开/关 | `editor_batch` / 内部调用不会碎成一堆撤销步 |
| 用户事务在场 | Begin 返回 false → **原样执行，不插队** | 否则 `EndTransaction` 会替用户关事务，破坏其撤销记录 |
| 回滚遇到用户步 | **立刻停**，报告 `stopped_reason` | 不跳过用户操作去撤更早的 agent 步（否则会误撤） |
| apply_graph | 写入段单独开事务，**编译前提交** | 编译必须在事务外；失败路径手工复原后 `Cancel`（不留空撤销步） |
| 面板 | **只读展示**（可回滚 N 步 / 已回滚 N 步） | 保持"管理端永久只读"铁律；回滚动作由 agent 命令触发 |

## 3. 命令面（白名单 84 → 86）

| 命令 | 入参 | 返回 |
|---|---|---|
| `agent_undo_status` | — | `{ok, active, depth, undo_count, redo_count, agent_undoable, next_title, next_is_agent}` |
| `agent_undo` | `count=1`（1–500） | `{ok, undone, titles[], stopped_reason, undo_count_before/after}` |

语义澄清（容易踩坑，已写入 `pitfalls.md` B18）：

- `undo_count` = **可撤销**条目数 = `GetQueueLength() - GetUndoCount()`；
- `redo_count` = 已撤销可重做数（UE 的 `GetUndoCount()` 名字有误导性）；
- `agent_undoable` = 撤销栈顶**连续**带 `[agent] ` 前缀的步数 —— 面板"可回滚 N 步"；
- `next_is_agent` = 下一步撤销是否 agent 步（能否安全回滚的唯一判据）。

`agent_undo` 在 `undone=0` 且存在 `stopped_reason` 时置 `success=false` +
`_fail_loud`（MCP `isError` 可见），绝不假成功。

## 4. 覆盖范围

**包事务（各留一步）**：图/节点/变量/组件/引脚/默认值/注释、材质表达式与连线、
UMG 子控件与属性、关卡 Actor 生成与变换、手术式连线/断线/删节点（MCP 直连路径）。

**不包（有意为之）**：

| 命令族 | 原因 |
|---|---|
| `compile_blueprint` / `compile_and_diagnose` / `build_blueprint` | 编译必须在事务外；资产创建/编译不属 UE 撤销语义 |
| `save_asset` / `delete_asset` / `unpin_console_refs` | 落盘与资产级操作不在撤销栈内（删除走高危确认） |
| PIE 生命周期 / 只读 / task_* | 不产生可撤销的编辑器状态 |

## 5. 面板

- 任务头部徽章：`⟲ 可回滚 N 步`（仅当 `agent_undoable>0`），悬停给出下一步标题与
  回滚口令；`已回滚 N 步`（任务账本里有回滚记录时）。
- 复盘卡片新增中性信息行：`⟲ 本任务已回滚 N 步(原因)`，不再误用"经验"样式。
- `/ui/api/task` 附带 `undo` 字段（2s 缓存 + 2s GT 超时：编辑器忙时面板不卡）。
- **不提供**"一键回滚"按钮：面板保持只读观测面；回滚由 agent 执行
  （对 AI 说"回滚这个任务的最近 N 步"即可）。

## 6. 真机验证（2026-10-27）

| 项 | 5.1（本地验证工程 A） | 5.8（本地验证工程 B） |
|---|---|---|
| 写命令自动留步 | ✅ `undo_count/agent_undoable=1`，标题 `[agent] add_comment_node …` | ✅ 同左 |
| `agent_undo 1` | ✅ `undone=1`，注释节点消失 | ✅ 同左 |
| `apply_graph` 内部事务 | ✅ 一步可回滚，撤销后节点消失 | —（5.1 已覆盖同一 C++ 分支） |
| `editor_batch` 两个子命令 | ✅ 两步独立可回滚 | — |
| 任务账本记录 | ✅ `rollbacks[{undone:1,titles:[…]}]` | ✅（面板截图同源） |
| `/ui/api/task.undo` | ✅ | ✅ |
| 状态一致性 | ✅ `active=false`、`redo_count` 递增 | ✅ |
| 面板渲染 | — | ✅ 徽章/复盘行/列表正常（截图核验） |

构建：5.1 BuildId `23058290`、5.8 BuildId `55116800`；产物分别落在
`bridge/BlueprintPythonBridge/Binaries/Win64`（5.1）与 `bridge/prebuilt-5.8/`。

## 7. 已知边界

1. **资产创建/删除不入撤销栈** —— 回滚后新建的蓝图仍在，删掉的资产不会回来。
   这是 UE 自身语义，不是本功能的疏漏（面板交付物 + `delete_asset` 覆盖该场景）。
2. **用户操作穿插即停** —— 若回滚序列中夹着用户自己的编辑，会停在用户步之前并
   报告原因；不会跳过。此时可先手动撤销用户步，或只回滚已完成的步数。
3. **同一命令内的编译在事务外** —— 若"写入 + 编译"分成两个命令，回滚只撤写入，
   编译状态由引擎自行重编译恢复（图已还原，下次编译自然一致）。
4. **面板不可直接回滚** —— 只读铁律的取舍；如需要按钮，须用户明确授权新增写路由。
