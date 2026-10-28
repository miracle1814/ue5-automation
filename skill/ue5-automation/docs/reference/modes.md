# 四种模式详细走查（§3.1–3.5）

> 从 SKILL.md §3.1–3.5 迁出（v4.6 分层）。硬规则仍在 SKILL.md 速查表；
> 本文件是完整步骤、参数与示例。

## 3.1 分析模式 — 读懂一个蓝图

**触发词：** `分析蓝图 {路径}` / `看看 {蓝图名}` / `帮我梳理 {路径}`

**参数：**

| 参数 | 必填 | 说明 | 示例 |
|:---|:---|:---|:---|
| `blueprint_path` | ✅ | 蓝图资产路径（Content Browser 右键 → Copy Reference） | `/Game/Blueprints/BP_EnemyController` |
| `depth` | ❌ | 分析深度：`basic` / `full`（默认 full） | `basic` 只出基本信息+变量清单 |
| `diagram_format` | ❌ | 流程图格式：`mermaid`（默认）/ `ascii` | 不支持 Mermaid 渲染时切换 ascii |

**工作流：**

```
你：分析蓝图 /Game/Blueprints/BP_MyActor
      │
      ▼
maintainer ──→ dev：L1 unreal API 读取类信息、变量、编译状态
      │          L2 C++ Bridge 读取节点/连线（Bridge API 覆盖 · 59 UFUNCTION）
      │
      ▼
docs-writer ──→ 按模板输出分析报告（7 个章节）
      │
      ▼
maintainer ──→ 汇总呈现给你
```

**输出物：** `📊 {蓝图名} 蓝图分析报告.md`（详见 [输出物说明](../SKILL.md#四输出物说明)）

---


## 3.2 创建模式 — 搭一个蓝图骨架

**触发词：** `创建一个蓝图用于 {用途}` / `帮我搭 {功能描述}` / `新建 BP {描述}`

**参数：**

| 参数 | 必填 | 说明 | 示例 |
|:---|:---|:---|:---|
| `description` | ✅ | 自然语言功能描述 | "可拾取物品，含拾取半径、物品类型枚举、拾取粒子特效" |
| `blueprint_type` | ❌ | 蓝图类型：`Actor`（默认）/ `Widget` / `FunctionLibrary` / `Component` | `Widget` |
| `parent_class` | ❌ | 父类，默认 AActor | `APawn` |
| `interfaces` | ❌ | 实现的接口列表 | `IInteractable, IDamageable` |

**工作流：**

```
你：创建一个可拾取物品蓝图，含拾取半径、物品类型枚举、拾取粒子特效
      │
      ▼
docs-writer ──→ 理解需求 → 输出流程方案（Step 1/2/3...）
      │
      ▼
你：确认 / 调整方案
      │
      ▼
dev ──→ HTTP → UE5：
          ✅ 创建蓝图资产
          ✅ 添加变量（含类型、默认值、Category）
          ✅ 添加组件
          ✅ 设置父类 / 接口
          ✅ 编译验证
      │
      ▼
maintainer ──→ 汇报结果 + 蓝图路径
```

**确认点：** 方案输出后**必须等你确认**才会动手创建，不会擅自操作 UE5 编辑器。

**能力边界：**

| v0.1 能做 ✅ | v0.1 暂不能 ❌（C++ Bridge 就绪后解锁） |
|:---|:---|
| 创建蓝图资产（Actor/Widget/FunctionLib） | 自动连接节点（Pin 连线） |
| 添加变量（类型+默认值+Category+Expose） | 创建自定义事件/函数图 |
| 添加组件（Component） | 从已有蓝图复制节点逻辑 |
| 设置父类、实现接口 | — |
| 编译验证，返回编译结果 | — |

**输出物：** `🏗️ {蓝图名} 创建方案.md`（方案阶段）+ 创建结果汇报（执行阶段）

---


## 3.3 诊断模式 — 查一个报错

**触发词：** `这个报错怎么回事` / `日志报错` / `帮我看看这段log` （后面贴日志）

**参数：**

| 参数 | 必填 | 说明 | 示例 |
|:---|:---|:---|:---|
| `log_text` | ✅ | UE 日志片段（支持多行） | 直接从 Output Log 复制粘贴 |
| `auto_fix` | ❌ | 是否自动修复：`true` / `false`（默认 true） | `false` 时仅诊断不修复 |

**工作流：**

```
你：贴日志
      │
      ▼
dev ──→ 解析日志：提取错误码、堆栈、关联蓝图/资产路径
      │
      ▼
dev ──→ 匹配错误模式库（error_patterns.json）
      │
      ├── 可自动修复（如缺引用、变量名拼写）
      │      └──→ dev直接修复 → 汇报修改了什么
      │
      └── 需人工判断
             └──→ docs-writer输出诊断报告（根因+建议+操作步骤）
```

**覆盖的错误类型（v0.1，15+ 模式）：**

| 类别 | 示例 | 自动修复？ |
|:---|:---|:---|
| 引用丢失 | `Referenced asset is null`、Missing Asset | ✅ |
| 编译错误 | `Blueprint could not be compiled`、Pin type mismatch | ⚠️ 诊断+建议 |
| 循环依赖 | 蓝图 A → B → A | ⚠️ 诊断+拓扑分析 |
| 接口不匹配 | Interface function not implemented | ✅ 补全签名 |
| 节点错误 | Cast failed、Node deprecated | ⚠️ 诊断+建议 |
| 性能告警 | Tick 耗时过长 | ⚠️ 诊断+优化建议 |

**输出物：** `🩺 诊断报告_{关联蓝图名}.md`

---


## 3.4 优化模式 — 评估整个项目

**触发词：** `分析项目 {路径}` / `项目架构评估` / `项目体检`

**参数：**

| 参数 | 必填 | 说明 | 示例 |
|:---|:---|:---|:---|
| `project_path` | ✅ | 项目 Content 目录 | `/Content/MyGame` |
| `scan_depth` | ❌ | 扫描深度：`quick` / `full`（默认 full） | `quick` 仅统计数量+TOP20热力图 |

**工作流：**

```
你：分析项目 /Content/MyGame
      │
      ▼
dev ──→ analyzer.py 扫描项目：
          ├── 遍历所有蓝图资产
          ├── 统计类型分布
          ├── 分析引用关系（热力图）
          └── 计算指标（复杂度/耦合度/继承深度/接口使用率）
      │
      ▼
docs-writer ──→ 输出架构评估报告：
          ├── 整体结构（数量+分布）
          ├── 架构指标（复杂度+耦合度+继承+接口）
          └── 问题清单（🔴阻断 🟡警告 🟢建议）
```

**输出物：** `📐 {项目名} 架构评估报告.md`

---


## 3.5 增量修改模式 — 编辑已有蓝图

**触发词：** `修改蓝图 {路径} {操作}` / `给 {蓝图} 加一个 {节点}` / `删除 {蓝图} 的 {节点}` / `把 {A} 连到 {B}`

**v0.7 新增。** 在已有蓝图上执行节点级编辑操作，不需要重新生成整个蓝图。

**核心 API（4 个）：**

| API | 用途 | 示例 |
|:---|:---|:---|
| `update_node_param(blueprint_path, node_id, param_name, new_value)` | 修改节点引脚默认值 | 改 PrintString 的 InString 为 "Hello" |
| `connect_pins_by_id(node_a, pin_a, node_b, pin_b)` | 按标识连接两个引脚 | BeginPlay.then → PrintString.execute |
| `disconnect_pin(node_id, pin_name)` | 断开引脚全部连线 | 断开 PrintString.InString 的输入 |
| `delete_node(node_id)` | 删除节点（含引用检查） | 删除一个废弃的 Delay 节点 |

**通信架构（增量修改专有）：**

```
Agent ── HTTP ──▶ ue5_bridge.py (UE 编辑器内 :8889)
                    ├── /read       → 读取当前状态（快照）
                    ├── /connect    → 连线（已有，含验证）
                    ├── /disconnect → 断线（v0.7 新增，GameThread 安全）
                    ├── /delete_node→ 删节点（v0.7 新增，含引用检查）
                    └── /command    → 白名单命令（set/get_pin_default_value 等；/execute 已废弃）
```

**工作流：**

```
你：修改 /Game/BP_Enemy 的 BeginPlay → PrintString 文本改为 "Enemy Spawned"
      │
      ▼
dev ──→ blueprint_editor.py：
          Step 1: freeze — 快照当前状态（/read）
          Step 2: execute — 逐个执行操作
            ├── update_node_param("PrintString", "InString", "Enemy Spawned")
            ├── disconnect_pin("OldEvent", "then")
            └── connect_pins_by_id("BeginPlay", "then", "NewEvent", "execute")
          Step 3: verify — 每个操作后验证生效
          Step 4: commit — 全部成功 → 记录事务日志
                   rollback — 任何失败 → 恢复到 freeze 快照
      │
      ▼
maintainer ──→ 汇报操作结果 + 事务日志路径
```

**安全机制：**

| 机制 | 说明 |
|:---|:---|
| **快照回滚** | 每操作前自动 freeze 状态，失败可恢复 |
| **P01 防假阳性** | 连线后双向 LinkedTo 验证（pitfalls #1） |
| **P02 防静默失败** | 前置参数校验 + 后置断言（pitfalls #2） |
| **跨图同属校验** | BreakLink 必须校验两 Pin 在同一图（KB 命中） |
| **GameThread 安全** | 所有 UE API 调用通过 `/disconnect` `/delete_node` 专有端点在 GameThread 执行 |
| **事务日志** | 每次操作写入 `logs/blueprint_editor.jsonl`，可审计 |

**Python 调用示例：**

```python
from blueprint_editor import BlueprintEditor

editor = BlueprintEditor()

# 1. 修改参数
result = editor.update_node_param(
    "/Game/BP_Enemy", "PrintString", "InString", "Enemy Spawned!"
)
print(result.details)  # ✅ PrintString.InString = Enemy Spawned! (已验证)

# 2. 断开旧连线
editor.disconnect_pin("/Game/BP_Enemy", "OldEvent", "then")

# 3. 建立新连线
editor.connect_pins_by_id(
    "/Game/BP_Enemy", "BeginPlay", "then", "NewEvent", "execute"
)

# 4. 删除废弃节点（可先干跑）
editor.delete_node("/Game/BP_Enemy", "DeprecatedNode", dry_run=True)
# → 🔍 干跑: DeprecatedNode — 4 引脚 (2 已连线)，可安全删除
editor.delete_node("/Game/BP_Enemy", "DeprecatedNode")
# → ✅ 已删除 DeprecatedNode（断开 2 条连线）
```

**CLI 快速调用：**

```bash
python blueprint_editor.py update /Game/BP_Test PrintString InString "Hello"
python blueprint_editor.py connect /Game/BP_Test BeginPlay then PrintString execute
python blueprint_editor.py disconnect /Game/BP_Test PrintString InString
python blueprint_editor.py delete /Game/BP_Test OldNode --dry-run
python blueprint_editor.py delete /Game/BP_Test OldNode
python blueprint_editor.py health
```

**输出物：**
- 操作结果直接返回（JSON 格式，含成功/失败/验证信息）
- 事务日志：`logs/blueprint_editor.jsonl`（每次操作一行 JSONL）
- 不生成 Markdown 报告（增量修改是即时操作，不是分析报告）

**测试状态（v0.7 FIX）：**
- ✅ 离线 Mock 测试：17/17 通过（`tests/test_blueprint_editor_mock.py`）
- ⏳ 端到端测试：待 UE 5.1 在线时执行（`tests/test_blueprint_editor_e2e.py`，pytest skip）

---


