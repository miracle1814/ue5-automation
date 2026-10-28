# UE5.1 自动化工具 — maintainer操作手册（入口集成）

> **文档定位：** 给maintainer（总调度 Agent）使用的五种模式完整调用链手册（A/B/C/D + 模式 E）  
> **版本：** v0.15（对齐 SKILL.md v0.15 · 2026-09-18 独立验证轮口径同步；v0.12 及更早条目见 SKILL.md 更新日志） · T-20260917-RELEASE-V218 · 附录 B 补注：`set_pin_default_value`/`get_pin_default_value` 支持 **GUID 定位**（`node_id` 或 `node_guid`）、`add_function_node` 建节点失败 **fail-loud** + 函数名候选；v0.11 条目见 SKILL.md 更新日志）  
> **日期：** 2026-09-17  
> **制定 Agent：** docs-writer（起草） / 小岑（v0.11 修订）  
> **前置文档：** SKILL.md / USER_GUIDE.md / templates/mermaid_tags.md / 全部模板  
> **一句话：** maintainer收到用户指令 → 查本手册 → 判定模式 → 按调用链分派 Agent → 汇总交付

---

## ⚠️ 重要：全部手动触发

健康检查 / 蓝图分析 / 蓝图创建 / 报错诊断 / 项目评估 —— **全部由用户在对话中手动触发**，不需要 cron，不需要定时任务。

maintainer的工作：**接收自然语言 → 匹配模式 → 按本手册分派 Agent → 汇总输出**。

---

## 模式速查表

| 模式 | 一句话 | 关键产出 | 耗时（预估） |
|:---|:---|:---|:---:|
| **A · 蓝图分析** | 给定蓝图路径，读懂它长什么样 | `analysis_report.md` | 30s–2min |
| **B · 蓝图创建** | 自然语言描述 → 自动搭骨架 | `creation_plan.md` + UE5 资产 | 1–3min（含确认） |
| **C · 报错诊断** | 贴日志 → 定位根因 → 给修复方案 | `diagnosis_report.md` | 5–30s |
| **D · 项目评估** | 全项目扫描 → 架构健康度报告 | `architecture_report.md` | 30s–5min |
| **E · 蓝图增量修改** | 修改已有蓝图：改参数 / 连(断)线 / 删节点 | `logs/blueprint_editor.jsonl`（即时 CLI 输出） | 秒级–1min（每操作即时返回） |

---

## 一、模式 A · 蓝图分析

### 1.1 触发词

用户说以下任意之一 → **判定为模式 A**：

| 类型 | 触发词示例 |
|:---|:---|
| **直接指令** | `分析蓝图 /Game/Blueprints/BP_XXX` |
| **自然语言** | `帮我看看 BP_EnemyController 里有什么`、`这个蓝图有哪些变量`、`画一下 BP_XXX 的流程图` |
| **路径格式** | 消息中包含 `/Game/` 路径 + `分析`/`看`/`读`/`检查`/`变量`/`节点`/`流程图` 等关键词 |

### 1.2 Agent 分工链

```
用户 → maintainer → dev → docs-writer → maintainer → 用户
              │
              └→ vision（L3 降级时）
```

### 1.3 完整调用链

```
Step 1 · maintainer（总调度）
  ├── 确认：用户给了蓝图路径吗？
  │   ├── 未给 → 追问："请提供蓝图的完整路径，例如 /Game/Blueprints/BP_MyActor"
  │   └── 已给 → 进入 Step 2
  └── 向下游发送：蓝图路径 + "执行蓝图分析，深度 full"

Step 2 · dev（Python/Bridge）
  ├── L1 尝试：通过 unreal Python API 读取蓝图元信息
  │   └── 调用：EditorAssetLibrary.load_asset() + get_class() + get_variables()
  │   └── 产出：{
  │         name, path, type, parent_class, interfaces,
  │         compile_status, last_modified, size_kb,
  │         variables: [{name, type, default, category, expose}, ...]
  │       }
  ├── L2 尝试（C++ Bridge 就绪后）：读取蓝图 Graph 节点和连线
  │   └── 调用：C++ Bridge → UEdGraphNode 遍历
  │   └── 产出：{
  │         nodes: [{type, label, graph, in_edges, out_edges}, ...],
  │         connections: [{from, to, type}, ...]
  │       }
  ├── L3 降级（L1+L2 均不可用）：通知maintainer → maintainer转vision
  │   └── vision：截图蓝图编辑器 → OCR + 节点识别 → 返回节点清单
  └── 将原始数据（JSON）交给docs-writer

Step 3 · docs-writer（内容输出）
  ├── 读取模板：templates/analysis_report.md
  ├── 填充数据：
  │   ├── 第一章：基本信息卡片（名称/类型/父类/接口/编译状态/最后修改）
  │   ├── 第二章：变量清单（名称/类型/默认值/暴露/Category）
  │   ├── 第三章：节点拓扑（节点类型分布饼图 + 核心节点清单 + 复杂度指标）
  │   ├── 第四章：Mermaid 流程图（引用 mermaid_tags.md 标签标准）
  │   ├── 第五章：引用资产（类型分组 + 完整性检查 + 引用拓扑图）
  │   ├── 第六章：关联蓝图（父子/CastTo/SpawnActor + 关联网络图 + 循环依赖检测）
  │   └── 第七章：优化建议（🔴阻断 / 🟡警告 / 🟢建议 + 优化路线）
  ├── 渲染 Mermaid：所有图遵守 templates/mermaid_tags.md v1.0（24标签+4线型+5色）
  └── 产出：analysis_report.md

Step 4 · maintainer（汇总交付）
  └── 将 analysis_report.md 发送给用户
  └── 附言："报告已生成。如需深入某个变量/节点，可以继续问。"
```

### 1.4 确认点

| 环节 | 确认内容 | 谁确认 |
|:---|:---|:---|
| Step 1 | 蓝图路径是否正确 | 用户 → maintainer |
| Step 3 | 报告生成完毕，无需额外确认 | — |

### 1.5 产出文件

| 文件 | 路径 | 内容 |
|:---|:---|:---|
| **蓝图分析报告** | 对话中直接输出（Markdown） | 七章完整报告，含 Mermaid 流程图 |
| 原始数据 | dev内存中传递（JSON） | 不对用户暴露 |

### 1.6 降级策略

| 级别 | 可用信息 | 报告中标注 |
|:---|:---|:---|
| L1 成功 | 基本信息 + 变量清单 | 数据来源：L1 `unreal` API |
| L2 成功 | + 节点拓扑 + 连线 | 数据来源：L2 C++ Bridge |
| L3 降级 | + 截图识别的节点（置信度标注） | 数据来源：L3 截图识别，置信度 N% |

---

## 二、模式 B · 蓝图创建

### 2.1 触发词

用户说以下任意之一 → **判定为模式 B**：

| 类型 | 触发词示例 |
|:---|:---|
| **直接指令** | `创建蓝图`、`新建蓝图`、`做一个蓝图` |
| **自然语言** | `我要一个可拾取物品蓝图，包含拾取半径、物品类型枚举、拾取粒子特效` |
| **特征匹配** | 消息中包含 `创建`/`新建`/`做`/`搭` + `蓝图`/`BP` + 功能描述 |

### 2.2 Agent 分工链

```
用户 → maintainer → docs-writer → maintainer → 用户（确认）
                     ↓（用户确认后）
               dev → maintainer → 用户
```

### 2.3 完整调用链

```
Step 1 · maintainer（总调度）
  ├── 确认：用户描述了蓝图需求吗？
  │   ├── 未描述 → 追问："你想创建什么样的蓝图？描述一下功能和需要的变量"
  │   └── 已描述 → 进入 Step 2
  └── 向下游发送：用户需求文本 → docs-writer

Step 2 · docs-writer（方案设计）【🔴 关键确认点】
  ├── 读取模板：templates/creation_plan.md
  ├── 输出方案：
  │   ├── 第一章：需求理解摘要（一句话 + 触发条件 + 生命周期 + 边界约束）
  │   ├── 第二章：文字步骤（Step 1/2/3...，含 API 调用说明）
  │   ├── 第三章：Mermaid 流程图（主流程 + 关键子图）
  │   ├── 第四章：节点/变量设计表（变量清单 + 事件/函数设计 + 接口设计）
  │   └── 第五章：用户确认清单（10 项检查）
  ├── 交给maintainer → maintainer呈现给用户
  └── ⚠️ 等待用户确认，不得跳过！

Step 3 · 用户确认
  ├── ✅ 用户说"确认"/"可以"/"没问题"/"就这样" → 进入 Step 4
  ├── 🔄 用户说"改一下XX" → docs-writer修改方案 → 再次呈现 → 等待确认
  └── ❌ 用户说"算了"/"取消" → 终止

Step 4 · dev（执行创建）
  ├── 读取docs-writer的 creation_plan.md → 提取创建指令
  ├── 通过 ue5_runner.py → UE5：
  │   ├── UE5Runner.build(spec)  → 创建蓝图资产 + 变量 + 组件 + 节点 + 编译
  │   └── UE5Runner.connect(...) → 自动连线（如有）
  ├── 记录实施结果：
  │   ├── 第六章：实施记录表（节点数/变量数/耗时/偏差）
  │   ├── 编译结果（✅/⚠️/🔴 + 警告详情）
  │   └── 偏差记录（如有）
  └── 更新 creation_plan.md 第六章并交给maintainer

Step 5 · maintainer（汇总交付）
  └── 将完整 creation_plan.md（含实施记录）发送给用户
  └── 附言："蓝图已创建 → /Game/Blueprints/BP_XXX。编译状态：✅/⚠️。节点连线请手动完成。"
  └── 若编译有警告 → 自动触发模式 C（报错诊断）排查
```

### 2.4 确认点

| 环节 | 确认内容 | 谁确认 |
|:---|:---|:---|
| Step 1 | 需求描述是否完整 | maintainer验证 |
| **Step 2 → 3** | **方案确认（必须！）** | **用户** |
| Step 4 | 无额外确认（自动执行） | — |

### 2.5 产出文件

| 文件 | 路径 | 内容 |
|:---|:---|:---|
| **创建方案** | 对话中输出（Markdown） | 需求理解 + 文字步骤 + 流程图 + 设计表 + 确认清单 + 实施记录 |
| **蓝图资产** | UE5 项目 `Content/` 目录 | 实际创建的 `.uasset` 文件 |
| 编译日志 | dev内存中 | 编译结果和警告详情 |

### 2.6 v0.11 能力边界（对齐 SKILL.md §2.3 API 清单 / §5.1 能力矩阵 v0.9 列）

| 能做 ✅ | 暂不能 ❌ |
|:---|:---|
| 创建蓝图资产（Actor / Widget / Function Library） | 从已有蓝图复制节点逻辑（Bridge API 无复制/克隆节点接口，能力矩阵无此行） |
| 添加变量（类型、默认值、Category、Expose） | — |
| 添加组件（Component） | — |
| 设置父类、实现接口 | — |
| 编译验证 | — |
| 自动连接节点（Pin 连线）——`ConnectPins`(17) + `connect_pins_by_id` | — |
| 创建自定义事件/函数图——`CreateEventGraph`(7) / `AddEventNode`(9) / `AddFunctionCallNode`(10) | — |

> 判定说明：右列 3 项原为 v0.1 口径的历史边界（当时等待 C++ 扩展就绪解锁）。v0.11 已就绪 → 按 SKILL.md §2.3 API 清单 + §5.1 能力矩阵逐项复核：「自动连接节点」由 API 17 `ConnectPins` / `connect_pins_by_id` 支撑 ✅；「创建自定义事件/函数图」由 API 7/9/10 支撑 ✅（能力矩阵 v0.2 起即 ✅）；「从已有蓝图复制节点逻辑」在 API 清单与能力矩阵中均无对应 → 仍不可行。

---

## 三、模式 C · 报错诊断

### 3.1 触发词

用户说以下任意之一 → **判定为模式 C**：

| 类型 | 触发词示例 |
|:---|:---|
| **贴日志** | 用户消息含 `LogBlueprint`/`LogScript`/`Error:`/`Warning:`/`Assertion failed` + 堆栈信息 |
| **直接指令** | `这个报错是什么意思`、`帮我看看这个错误`、`编译报错了` |
| **自然语言** | `蓝图编译失败了`、`运行时 Crash`、`引用丢失` + 具体错误描述 |
| **链接触发** | 模式 B 创建后编译失败 → maintainer自动触发模式 C |

### 3.2 Agent 分工链

```
用户 → maintainer → dev → docs-writer → maintainer → 用户
              │（日志解析+模式匹配）
              └→ 命中 → dev自动修复（需确认）
              └→ 未命中 → docs-writer出 diagnosis_report
```

### 3.3 完整调用链

```
Step 1 · maintainer（总调度）
  ├── 确认：用户贴了 UE 日志片段吗？
  │   ├── 未贴 → 追问："请把完整的报错日志贴给我，包含 [时间戳] 和 Error 行"
  │   └── 已贴 → 进入 Step 2
  └── 向下游发送：完整日志文本 → dev

Step 2 · dev（日志解析 + 模式匹配）
  ├── 解析日志 → 提取结构化摘要：
  │   ├── 时间戳、日志类别（LogBlueprint/LogScript/...）
  │   ├── 错误码/关键词
  │   ├── 关联蓝图路径
  │   ├── 关联资源路径
  │   └── 调用栈（如有）
  ├── 匹配错误模式库（error_patterns.json）：
  │   ├── 高置信度（>80%）：日志关键词 + 调用栈双重匹配
  │   ├── 中置信度（50-80%）：仅日志关键词匹配
  │   └── 低置信度（<50%）：部分特征相似 → 需人工确认
  ├── 判断自动修复能力：
  │   ├── ✅ 可自动修复 → 进入 Step 3a
  │   └── ⚠️/❌ 需人工 → 进入 Step 3b
  └── 将解析结果 + 匹配结果交给docs-writer

Step 3a · dev（自动修复）【🔴 需用户确认】
  ├── 条件：错误模式标记为 auto_fixable = "yes" 或 "partial"
  ├── 类型示例：
  │   ├── 引用丢失 → 扫描替换路径
  │   ├── 接口不匹配 → 自动补全函数签名
  │   └── 变量名拼写 → 自动修正
  ├── ⚠️ 修复前必须向用户确认：
  │   └── "检测到 X 问题，我可以自动修复：[具体操作]。是否执行？"
  ├── 用户确认 → 执行修复 → 编译验证 → 汇报结果
  └── 用户拒绝 → 输出手动修复建议

Step 3b · docs-writer（诊断报告）
  ├── 读取模板：templates/diagnosis_report.md
  ├── 填充报告：
  │   ├── 第一章：日志摘要（原始日志 + 结构化摘要 + 严重级别 + 时间线）
  │   ├── 第二章：错误定位（文件:行号 + 调用栈 Mermaid 图 + 影响范围）
  │   ├── 第三章：根因分析（模式匹配 + 详细技术解释 + 置信度）
  │   ├── 第四章：修复方案（策略对比 + 分步操作 + Python 自动修复脚本）
  │   ├── 第五章：验证步骤（验证清单 + 回归测试 + 通过标准）
  │   └── 第六章：诊断元数据
  └── 产出：diagnosis_report.md

Step 4 · maintainer（汇总交付）
  ├── 情况 A（自动修复成功）：告知用户修复结果 + 编译状态
  ├── 情况 B（需手动）：交付 diagnosis_report.md + "按第 4 章步骤操作即可"
  └── 附言："修复后如需验证，我可以帮你编译检查。"
```

### 3.4 确认点

| 环节 | 确认内容 | 谁确认 |
|:---|:---|:---|
| Step 1 | 日志片段是否完整 | maintainer验证 |
| **Step 3a** | **自动修复方案确认（必须！）** | **用户** |
| Step 3b | 无需确认（直接出报告） | — |

### 3.5 产出文件

| 文件 | 路径 | 内容 |
|:---|:---|:---|
| **诊断报告** | 对话中输出（Markdown） | 六章完整报告，含错误定位 Mermaid 图 + Python 修复脚本 |
| 自动修复结果 | UE5 项目中 | 已修复的蓝图资产 |
| 错误模式匹配日志 | dev内存中 | 置信度 + 模式 ID + 匹配分数 |

### 3.6 错误模式库分类

| 类别 | 模式数 | 典型关键词 | 自动修复 |
|:---|:---:|:---|:---:|
| **引用丢失** | 5 | `Referenced asset is null`、`Missing Asset` | ✅ |
| **编译错误** | 4 | `Blueprint could not be compiled`、`type … not found` | ⚠️ |
| **循环依赖** | 2 | `Circular dependency`、`Redirect loop` | ❌ |
| **接口不匹配** | 2 | `Interface function not implemented` | ✅ |
| **蓝图节点错误** | 3 | `Cast failed`、`Node deprecated` | ⚠️ |
| **性能告警** | 2 | `Tick took`、`GC overhead` | ❌ |

---

## 四、模式 D · 项目评估

### 4.1 触发词

用户说以下任意之一 → **判定为模式 D**：

| 类型 | 触发词示例 |
|:---|:---|
| **直接指令** | `分析项目架构`、`评估架构`、`扫描项目` |
| **自然语言** | `我的项目架构健康吗`、`有哪些蓝图太大了`、`有没有循环依赖` |
| **路径格式** | 含 `/Content/` 或项目目录路径 + `架构`/`评估`/`扫描`/`健康度`/`分析` 等关键词 |

### 4.2 Agent 分工链

```
用户 → maintainer → dev（全量扫描）→ docs-writer → maintainer → 用户
```

### 4.3 完整调用链

```
Step 1 · maintainer（总调度）
  ├── 确认：用户指定了项目路径吗？
  │   ├── 未指定 → 询问："请指定要分析的 UE5 项目路径，如 /Content/MyGame"
  │   └── 已指定 → 进入 Step 2
  └── 向下游发送：项目路径 + "全量架构扫描"

Step 2 · dev（全量扫描）
  ├── 调用 analyzer.py（全量扫描脚本）：
  │   ├── 蓝图数量统计 + 类型分布（Actor/Widget/Component/Interface/...）
  │   ├── 目录结构与各目录蓝图密度
  │   ├── 节点数分布（0-50 / 51-100 / 101-200 / 201-300 / 300+）
  │   ├── Top 20 大蓝图（按节点数降序）
  │   ├── 超 200 节点蓝图详细清单（含拆分建议）
  │   ├── 跨目录引用统计（同目录/近/远/跨模块）
  │   ├── 引用热力图 Top 20（被引用次数最多的蓝图）
  │   ├── 循环依赖检测（拓扑排序 + 环识别）
  │   ├── 继承深度分布 + 深度 > 4 的类继承链
  │   └── 接口使用率 + 抽象类占比
  ├── 产出：原始数据 JSON（含所有统计量）
  └── 交给docs-writer

Step 3 · docs-writer（架构报告）
  ├── 读取模板：templates/architecture_report.md
  ├── 填充数据：
  │   ├── 第一章：架构全景（蓝图总数 + 类型分布饼图 + 目录树 + 密度热力图）
  │   ├── 第二章：复杂度指标（节点数分布 + Top 20 大蓝图 + 复杂度总览）
  │   ├── 第三章：耦合度分析（跨目录引用 + 引用热力图 + 循环依赖 + 耦合评分）
  │   ├── 第四章：继承分析（深度分布 + 过深链 + 继承树 + 接口使用率）
  │   ├── 第五章：问题清单（🔴阻断 / 🟡警告 / 🟢建议 + 严重度分布）
  │   ├── 第六章：优化路线图（优先级排序 + 收益/代价矩阵 + 阶段里程碑 + Gantt 图）
  │   └── 第七章：评估元数据（架构健康评分卡 + 综合评分）
  ├── 所有图表遵守 templates/mermaid_tags.md v1.0
  └── 产出：architecture_report.md

Step 4 · maintainer（汇总交付）
  └── 将 architecture_report.md 发送给用户
  └── 附言："架构评估完成，综合评分 N.N/10。建议优先处理 🔴 阻断级问题。"
```

### 4.4 确认点

| 环节 | 确认内容 | 谁确认 |
|:---|:---|:---|
| Step 1 | 项目路径是否正确 | 用户 → maintainer |
| Step 3 | 报告生成完毕，无需额外确认 | — |

### 4.5 产出文件

| 文件 | 路径 | 内容 |
|:---|:---|:---|
| **架构评估报告** | 对话中输出（Markdown） | 七章完整报告，含 10+ Mermaid 图表（饼图/象限图/柱状图/依赖网络/甘特图） |
| 原始扫描数据 | dev内存中（JSON） | 不对用户暴露 |

### 4.6 扫描性能预估

| 蓝图数量 | 预计耗时 | 说明 |
|:---|:---:|:---|
| < 50 | 5–15s | 小型项目 |
| 50–150 | 15–45s | 中型项目 |
| 150–300 | 30s–2min | 大型项目 |
| 300+ | 1–5min | 超大型项目，建议指定子目录 |

> v0.2 支持增量扫描（只扫变更蓝图），v0.3 支持后台异步执行 + 进度推送。

---

## 模式 E · 蓝图增量修改调度链

### E.1 触发词

用户说以下任意之一 → **判定为模式 E**：

| 类型 | 触发词示例 |
|:---|:---|
| **直接指令** | `修改蓝图 /Game/Blueprints/BP_XXX`、`给 BP_XXX 加一个节点`、`删除 BP_XXX 的某个节点` |
| **自然语言** | `把 BP_Enemy 的打印文本改一下`、`把 A 事件连到 B 函数`、`断开 BP_XXX 这条连线` |
| **特征匹配** | 消息中包含 `/Game/` 路径 + `修改`/`加`/`删`/`连`/`断开`/`改参数` 等编辑类关键词（区别于模式 A 的只读分析） |

### E.2 Agent 分工链

```
用户 → maintainer → dev → maintainer → 用户
              │
              └→ blueprint_editor.py → ue5_bridge.py（:8889 写入通道）
```

### E.3 完整调用链

```
Step 1 · maintainer（总调度）
  ├── 确认：用户给了蓝图路径 + 明确编辑操作吗？
  │   ├── 缺路径 → 追问："请提供蓝图完整路径，如 /Game/Blueprints/BP_MyActor"
  │   └── 缺操作 → 追问："要对它做什么修改？（改参数 / 连线 / 断线 / 删节点）"
  └── 向下游发送：蓝图路径 + 编辑指令 → dev

Step 2 · dev（增量修改执行）
  ├── 调用 blueprint_editor.py（4 个核心 API）：
  │   ├── update_node_param(node_id, param_name, val)  → 修改节点引脚默认值
  │   ├── connect_pins_by_id(node_a, pin_a, node_b, pin_b) → 按标识连接两个引脚
  │   ├── disconnect_pin(node_id, pin_name)             → 断开引脚全部连线
  │   └── delete_node(node_id)                          → 删除节点（含引用检查）
  ├── 保护流程：freeze 快照 → execute 逐操作 → verify 每个操作后验证 → commit/rollback
  ├── 底层走 ue5_bridge.py（UE 编辑器内 :8889 写入通道）
  │   ├── /read        → 读取当前状态（快照）
  │   ├── /connect     → 连线（含验证）
  │   ├── /disconnect  → 断线（GameThread 安全）
  │   ├── /delete_node → 删节点（含引用检查）
  │   └── /execute     → 通用 Python 执行（改参数等）
  └── 产出：操作结果 JSON + 事务日志 logs/blueprint_editor.jsonl

Step 3 · maintainer（汇总交付）
  ├── 将操作结果 JSON 转述给用户（成功/失败/验证信息）
  └── 附言："修改完成 → /Game/Blueprints/BP_XXX。事务日志已记录。"
```

### E.4 确认点

| 环节 | 确认内容 | 谁确认 |
|:---|:---|:---|
| Step 1 | 蓝图路径 + 编辑操作是否明确 | 用户 → maintainer |
| Step 2 | 无额外确认（自动执行，含快照回滚保护） | — |

### E.5 产出文件

| 文件 | 路径 | 内容 |
|:---|:---|:---|
| 操作结果 | 对话中直接输出（JSON） | 成功/失败/验证信息（增量修改是即时操作，不生成 Markdown 报告） |
| 事务日志 | `logs/blueprint_editor.jsonl` | 每次操作一行 JSONL：操作类型/蓝图路径/参数/结果/耗时（可审计） |

### E.6 安全机制

| 机制 | 说明 |
|:---|:---|
| **快照回滚** | 每操作前自动 freeze 状态，失败可恢复 |
| **P01 防假阳性** | 连线后双向 LinkedTo 验证（pitfalls #1） |
| **P02 防静默失败** | 前置参数校验 + 后置断言（pitfalls #2） |
| **跨图同属校验** | BreakLink 必须校验两 Pin 在同一图（KB 命中） |
| **GameThread 安全** | 所有 UE API 调用通过 `/disconnect` `/delete_node` 专有端点在 GameThread 执行 |
| **事务日志** | 每次操作写入 `logs/blueprint_editor.jsonl`，可审计 |

> ⚠️ 模式 E 只做**已有蓝图的节点级增量修改**，不做整图重建；「从已有蓝图复制节点逻辑」仍不可行（见 §2.6）。若需要分析蓝图结构 → 模式 A；需要创建全新蓝图 → 模式 B。

---

## 五、跨模式规则

### 5.1 模式联动

| 触发场景 | 自动联动 |
|:---|:---|
| 模式 B 创建后编译失败 | → 自动触发模式 C（报错诊断） |
| 模式 E 修改后编译失败 | → 自动触发模式 C（报错诊断） |
| 模式 C 诊断中发现循环依赖 | → 建议触发模式 D（项目架构评估） |
| 模式 D 发现某蓝图问题严重 | → 建议对该蓝图触发模式 A（深入分析） |
| 模式 A 发现变量类型缺失 | → 建议触发模式 C（报错诊断） |

### 5.2 模式判定优先级

当用户消息同时匹配多个模式时，按以下优先级：

```
1. 模式 C（报错诊断）     — 消息含 UE 日志/Error/Warning 格式
2. 模式 A（蓝图分析）     — 消息含 /Game/ 蓝图路径 + "分析"/"看"（只读分析）
3. 模式 B（蓝图创建）     — 消息含 "创建"/"新建" + 功能描述
4. 模式 E（蓝图增量修改） — 消息含 /Game/ 蓝图路径 + 编辑动词（"修改"/"加"/"删"/"连"/"断开"/"改参数"）
5. 模式 D（项目评估）     — 消息含 "架构"/"评估"/"扫描" + 项目路径
```

> **补 E 位置依据（T-20260909-SKILL-P2-MODE-E · v0.11）：** E 排在 B 之后——E 以「已有蓝图」为操作前提，B 负责创建蓝图；消息同时含创建词与编辑词时（如「创建 BP_X 并把 A 连到 B」）蓝图尚未存在，应整体按 B 走创建流程。E 不插在 A 之前——A 为只读分析、E 为蓝图写操作（§E.1 触发词即「区别于模式 A 的只读分析」），消息同时含只读词与编辑词时先按 A 交付只读分析、用户确认后再走 E，与「评估 ≠ 执行」原则一致。E 为单蓝图级即时操作，与 A/B 同级，先于项目级 D，延续「单点优先于全局」的既有次序（C 因报错紧急、日志特征强仍居首）。

### 5.3 超时与探活

| 场景 | 动作 |
|:---|:---|
| 预估 > 5 分钟的任务 | maintainer每 5 分钟向执行 Agent 探活 |
| dev扫描超时（如 200+ 蓝图 > 5min） | maintainer通知用户 + 建议缩小扫描范围 |
| docs-writer报告生成 > 3min | maintainer先交付已完成的章节（分批输出） |

---

## 六、文件索引

### 6.1 所有已完成文件（Tier 1 + 模板）

| 文件 | 路径 | 用途 |
|:---|:---|:---|
| SKILL.md | `skills/ue5-automation/SKILL.md` | 立项技术文档（模块设计 + 技术架构 + 分阶段计划） |
| USER_GUIDE.md | `skills/ue5-automation/USER_GUIDE.md` | 用户上手卡（一句话定位 + 5 场景一句话入口 + 前提条件 + 卡住速查） |
| workflow.md | `skills/ue5-automation/workflow.md` | **本文件** — maintainer操作手册 |
| analysis_report.md | `templates/analysis_report.md` | 模式 A 输出模板（蓝图分析报告） |
| creation_plan.md | `templates/creation_plan.md` | 模式 B 输出模板（蓝图创建方案） |
| diagnosis_report.md | `templates/diagnosis_report.md` | 模式 C 输出模板（报错诊断报告） |
| architecture_report.md | `templates/architecture_report.md` | 模式 D 输出模板（架构评估报告） |
| mermaid_tags.md | `templates/mermaid_tags.md` | Mermaid 标签标准（24标签+4线型+5色；仅 templates/ 一份，报告模板引用此路径） |

### 6.2 脚本 / 组件文件索引（原 v0.2+ 待实现清单 · v0.11 已全部实现落地）

| 文件 | 实际路径 | 说明 | 状态 |
|:---|:---|:---|:---:|
| `ue5_runner.py` | `scripts/ue5_runner.py` | **UE5 统一执行器**（ue_send 直调 + HTTP bridge 自动启动；health/read/build/connect CLI） | ✅ 在用 |
| `error_patterns.json` | `config/error_patterns.json` | 错误模式库（**19 条** · PTN-001~019 · pitfalls P01–P19 同源；2026-09-17 `json` 实测订正，原记「40+ 模式 / P01–P16」） | ✅ 在用 |
| `analyzer.py` | `scripts/analyzer.py` | 项目架构扫描脚本（模式 D Step 2 调用） | ✅ 在用 |
| `blueprint_editor.py` | `scripts/blueprint_editor.py` | 增量修改引擎（v0.7：4 核心 API + freeze/execute/verify + 事务日志） | ✅ 在用 |
| C++ Bridge Plugin | 发布包 `bridge/BlueprintPythonBridge/`（skill 源根无此目录；部署后位于 `<Project>/Plugins/BlueprintPythonBridge/`，见 DEPLOY.md） | UE C++ 插件（59 UFUNCTION · DLL 828,416B · 2026-09-10 编译；在线实测闭环） | ✅ 在用 |
| `ue5_bridge.py` | `scripts/ue5_bridge.py` | UE5 侧 FastAPI Bridge 服务端（写入通道主入口 :8889；/disconnect /delete_node v0.7 新增） | ✅ 在用 |

---

## 附录 A：maintainer快速参考卡

### A.1 一句话判定

| 用户说了什么 | 模式 | 先把任务发给谁 |
|:---|:---:|:---|
| "分析蓝图 /Game/..." | **A** | dev |
| "创建一个 XX 蓝图" | **B** | docs-writer（出方案） |
| "报错了，这是日志..." | **C** | dev（解析日志） |
| "分析项目架构 /Content/..." | **D** | dev（全量扫描） |
| "修改蓝图 /Game/BP_XXX 的 Y" | **E** | dev（增量修改引擎） |

### A.2 模式 B 的特殊规则

> ⚠️ 模式 B 是最容易出错的模式。记住：
> 1. docs-writer出方案 → **用户必须确认** → dev才执行
> 2. dev执行完 → 编译验证 → 如果失败 → 自动触发模式 C
> 3. v0.11 已支持节点级增量修改：4 个 API —— `update_node_param`（改参数）/ `connect_pins_by_id`（连线）/ `disconnect_pin`（断线）/ `delete_node`（删节点），已有蓝图无需整体重建即可增量编辑（见模式 E）

### A.3 降级规则

> 当某个 Agent 不可用或超时时：
> - dev不可用 → vision（截图为后备） → docs-writer出报告
> - vision不可用 → docs-writer标注「数据来源受限」并仅输出已有信息
> - docs-writer不可用 → maintainer直接汇总dev/vision的原始数据给用户

---

> **文档版本：** v0.11 · 2026-09-09 · docs-writer（调研写作 Agent）起草 / 小岑（T-20260909-SKILL-DOC-USERGUIDE）修订  
> **依赖：** SKILL.md / USER_GUIDE.md / templates/mermaid_tags.md / 全部 4 个模板  
> **下次更新：** ✅ 已完成——① 补 SKILL.md §3.5 蓝图增量修改（模式 E）调度链章节（T-20260909-SKILL-V26-DOCFIX · 2026-09-09 落地，含 A.1 速查表 E 行 / A.2 第 3 条改写 / §2.6 能力边界 v0.11 对齐）；② SKILL.md §5.2 已知限制表补模式 E 限制 + workflow §5.2 模式判定优先级补模式 E 条目（T-20260909-SKILL-P2-MODE-E · 2026-09-09 落地）；③ workflow.md §5.1 模式联动表补模式 E 行——模式 E 修改后编译失败 → 自动触发模式 C（T-20260909-SKILL-P2-MODE-E-S51 · 2026-09-09 落地）。下一项候选：暂无（待新需求再派单）。

---

## 附录 B：L2 命令速查（`/command` 白名单 **72 条** (v3.0) · 2026-09-12 在线实测 + 2026-09-13 新增落盘 + 2026-09-17 补登 2 条）

> **来源**：报告 §1「A 项 21 项逐项表」（run_4 = 唯一基线 · `71 passed + 1 xfailed / 0 failed` · 72.42s）· 白名单实装 = `scripts\ue5_bridge.py::_COMMAND_WHITELIST`（**50 键** · 2026-09-17 `ast` 实测订正 · 原记 48 键）。
> **用法**：`POST http://127.0.0.1:8889/command` · 头 `X-Skill-Token` · 体 `{"command": "<命令>", "params": {...}}`。未登记命令 → `Unknown command`（fail-loud）。
> **详表**：`docs\L2-COMMANDS.md`（21 条 L2 逐条签名/入参/正负路径/raw 锚点）· `SKILL.md` §2.3（命令 × C++ API 对照）。

| # | `command` | 分组 | 对应 C++ API | 说明 / 在线实测 |
|:--:|:---|:---|:---|:---|
| 1 | `health` | 基础 15 | GetUEInfo | health |
| 2 | `list_assets` | 基础 15 | — | 列资产 |
| 3 | `read_blueprint` | 基础 15 | — | 读蓝图 |
| 4 | `read_variables` | 基础 15 | ReadVariables(#26) | 读变量 |
| 5 | `read_cdo` | 基础 15 | — | 读 CDO |
| 6 | `read_nodes` | 基础 15 | ListNodes(#13) + ListPins(#14) | 读节点（R3：`graph_name` 有值 → 只返回该图） |
| 7 | `read_connections` | 基础 15 | GetNodeConnections(#19) | 读连线拓扑 |
| 8 | `build_blueprint` | 基础 15 | CreateActorBlueprint(#1) 等组合 | 建蓝图（兼容旧调用） |
| 9 | `add_variable` | 基础 15 | AddMemberVariable(#4) | 加成员变量 |
| 10 | `add_component` | 基础 15 | AddComponentToBlueprint(#24) | 加组件 |
| 11 | `add_function_node` | 基础 15 | AddFunctionCallNode(#10) | 加函数调用节点（建节点失败 → **必抛 `BridgeError`** + 函数名候选，不再假阳性成功 · v0.13） |
| 12 | `connect_pins` | 基础 15 | ConnectPins(#17) | 连引脚 |
| 13 | `compile_blueprint` | 基础 15 | CompileBlueprint(#2) | 编译蓝图 |
| 14 | `set_pin_default_value` | 基础 15 | SetPinDefaultValue(#18) | 设引脚默认值（`node_id` **兼收标题与 32 位 GUID**，亦可 `node_guid`；返回 `matched_by` · v0.13） |
| 15 | `get_pin_default_value` | 基础 15 | — | 读引脚默认值（同上 · GUID 定位 · v0.13） |
| 16 | `create_node_by_class` | L1 11 | AddNodeByClass(#28) | 通用建节点 |
| 17 | `set_node_position` | L1 11 | SetNodePosition(#29) | 设节点坐标 |
| 18 | `get_node_by_guid` | L1 11 | GetNodeByGuid(#30) | 按 GUID 查节点（在线复验：正路径命中 + 伪造/畸形/空 GUID 全 fail-loud） |
| 19 | `add_variable_node` | L1 11 | AddVariableNode(#32) | 变量 Get/Set 节点 |
| 20 | `add_custom_event_node` | L1 11 | AddCustomEventNode(#44) | 自定义事件 |
| 21 | `add_input_key_event_node` | L1 11 | AddInputKeyEventNode(#39) | 按键事件 |
| 22 | `add_function_pin` | L1 11 | AddFunctionPinEntry(#50) | 函数图参数引脚 |
| 23 | `add_local_variable` | L1 11 | AddLocalVariable(#51) | 函数图局部变量 |
| 24 | `add_timeline_node` | L1 11 | AddTimelineNode(#53) | Timeline 节点 |
| 25 | `add_comment_node` | L1 11 | AddCommentNode(#52) | 注释框 |
| 26 | `set_class_default` | L1 11 | SetClassDefault(#57) | 写类默认值 |
| 27 | `add_cast_node` | **L2 21** | AddCastNode(#33) | ✅ `title="类型转换为 Actor"` · 608ms |
| 28 | `add_class_cast_node` | **L2 21** | AddClassCastNode(#34) | ✅ `title="类型转换为 Actor类"` · 594ms |
| 29 | `add_macro_node` | **L2 21** | AddMacroNode(#35) | ✅ `title="For Each Loop"` · 619ms；**24 候选宏名全扫 `ok=24 / fail=0`** |
| 30 | `add_switch_node` | **L2 21** | AddSwitchNode(#36) | ✅ `int` → `切换整型`；`enum` → `切换ECollisionChannel` / `切换EInputEvent` |
| 31 | `add_struct_node` | **L2 21** | AddStructNode(#37) | ✅ `make` → `"创建Vector"`；`break` → `"中断Vector"` |
| 32 | `add_enum_literal_node` | **L2 21** | AddEnumLiteralNode(#56) | ✅ `title="文字枚举ECollisionChannel"` ×2 |
| 33 | `add_composite_node` | **L2 21** | AddCompositeNode(#54) | ✅ `title="EdGraph_0\n合并的图表"`（`pins` 回读为空 → 已留 warning，不静默） |
| 34 | `add_math_expression_node` | **L2 21** | AddMathExpressionNode(#38) | ✅ `title="(A + B) * C\n数学表达式"` · 653ms |
| 35 | `add_async_action_node` | **L2 21** | AddAsyncActionNode(#55) | ✅ `title="延迟"` · 628ms（ProxyFactory 反射写入） |
| 36 | `add_create_delegate_node` | **L2 21** | AddCreateDelegateNode(#45) | ✅ `title="创建事件"` · 590ms |
| 37 | `add_actor_bound_event_node` | **L2 21** | AddActorBoundEventNode(#43) | ✅ `title="Actor开始重叠时 (StaticMeshActor0)"` |
| 38 | `add_input_axis_event_node` | **L2 21** | AddInputAxisEventNode(#41) | ✅ 按键轴 `"W"`；命名轴 `"输入轴Move Forward / Backward"` |
| 39 | `add_input_action_event_node` | **L2 21** | AddInputActionEventNode(#40) | ✅ `title="输入操作Jump"` |
| 40 | `add_component_bound_event_node` | **L2 21** | AddComponentBoundEventNode(#42) | ✅ `title="组件开始重叠时 (PywrapBox)"` |
| 41 | `add_pin_to_node` | **L2 21** | AddPinToNode(#47) | ✅ `ok=true` |
| 42 | `remove_pin_from_node` | **L2 21** | RemovePinFromNode(#48) | ✅ `ok=true · removed=true` |
| 43 | `try_node_add_input_pin` | **L2 21** | TryNodeAddInputPin(#49) | ⚠️ xfail ⚠️ **XFAIL**（引擎层不支持：UE5.1 `K2Node_SwitchInteger` 未实现 `IK2Node_AddPinInterface`） |
| 44 | `set_node_property` | **L2 21** | SetNodeProperty(#31) | ✅ `NodeComment` / `bCanRenameNode` 均 `ok=true` |
| 45 | `remove_component` | **L2 21** | RemoveComponent(#58) | ✅ `ok=true`（`removed` 回读不可用 → `warnings[2]`，不静默） |
| 46 | `add_implemented_interface` | **L2 21** | AddImplementedInterface(#59) | ✅（含 R3 硬化） 加固前实测 `ok=true` 330ms（D-1 族入口） |
| 47 | `add_event_dispatcher` | **L2 21** | AddEventDispatcher(#46) | ✅ `ok=true` |
| 48 | `save_asset` | 落盘 1 | SaveAsset | ✅ `ok=true` · 磁盘 .uasset mtime 变化（T-20260913-UE5BRIDGE-SAVE-ASSET 新增） |
| 49 | `delete_asset` | 资产 2 | —（EditorAssetLibrary + AssetRegistry 组合） | ✅ 级联删除 + **保存作用域已收窄**（D-7 · T-20260917-D7-DELETE-SCOPE-FIX）：仅保存**引用被删资产的包**，⛔ 不再全工程 `save_dirty_packages`；返回体含 `save_scope` / `saved_related_packages` / `save_skipped_reason` |
| 50 | `unpin_console_refs` | 资产 2 | —（Python 侧 console 引用表） | ✅ 解除指向目标资产的 console 引用 · `asset_path` 默认 `""`（D-4）→ 空参 / 无参返回**可读错误**且**不执行任何删除**（安全硬边界未放宽） |

**总计 72 条**（15 + 11 + 21 + 1 落盘 + 2 资产 + 9 v3.0 + 9 材质 + 4 UMG）· L2 21 条 = **20 ✅ + 1 ⚠️(xfail)**。L2 逐条正/负路径原文见 output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\L2-在线联调报告-20260912.md §1–§2，raw 原文见 `output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\raw\`。

> ⚠️ `try_node_add_input_pin` = 引擎层不支持（UE5.1 `K2Node_SwitchInteger` 未实现 `IK2Node_AddPinInterface`）→ **不得依赖其成功**。
> 🔴 路径类入参（`target_class_name` / `struct_name` / `enum_name` / `factory_class_name` / `interface_class_path`）必须传**可加载**路径；不可解析（含裸短名）→ 守卫 fail-loud（`拒绝调用引擎`）——引擎侧 `check()` 会硬崩编辑器。
