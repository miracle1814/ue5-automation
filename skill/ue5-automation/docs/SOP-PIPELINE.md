# SOP-PIPELINE · UE5 全自动蓝图开发流水线

> **版本**：v1.0 · 2026-09-09 定稿
> **定位**：把「用户一句自然语言需求」变成「可验收的 UE 蓝图资产 + 关卡实例 + 实操指南」的标准作业程序。
> **适用**：`skills/ue5-automation` 覆盖的蓝图创建 / 读取 / 编辑 / 编译 / 关卡摆放链路。
> **不适用**：C++ 模块开发、渲染管线、Nanite/Lumen 调参、美术资产制作（模型 / 贴图 / 动画）。

---

## 0 · 一图流

```
用户自然语言需求
   ↓  P0 需求规格化
规格书（功能点 R1..Rn + 数值表 + 验收口径）
   ↓  P1 能力预检（实读源码 / schema / 在线实测，不读文档字符串）
三色能力清单（🟢 全自动 / 🟡 半自动 / 🔴 射程外）
   ↓  P2 方案设计
架构说明 + Spec JSON 契约 + 验收表 + 拍板点
   ↓  P3 双签门          ← 🔴 硬卡点：未签字不得动手
用户签字（或明确「执行 / 动手 / 做它」）
   ↓  P4 快照 → 执行 → 逐步回读
蓝图资产 + 关卡实例 + 执行日志
   ↓  P5 自动验收 + 错误排查
🤖 验收报告（逐项实测证据）
   ↓  P6 交付
产物清单 + 用户实操测试指南 + 遗留项
```

**三条核心原则**

1. **「严丝合缝」靠 schema 实读，不靠文档字符串。**
   实测案例：`ue5_runner.py` 的 build spec 文档串只列 `variables/events/functions/connections`，而 `ue5_bridge.py` L1913-1917 的 `BuildRequest` 实际还支持 `components` / `interfaces` / `blueprint_type` / `compile_after`，且 L3188-3192 真的会遍历 `spec.components`。**文档滞后于实现，契约必须以源码为准。**
2. **能力边界先摊开再动手。**
   射程外的需求必须在 P2 就降级或砍掉，禁止「做到一半发现做不了」。
3. **验收证据必须可复现。**
   每条「已完成」附实测命令 + 输出，禁止「理论上应该成功」。

---

## 1 · 三色能力分级

| 色 | 含义 | 处理方式 |
|:--:|:---|:---|
| 🟢 **全自动** | skill 独立完成，零人工介入 | 直接进 Spec |
| 🟡 **半自动** | skill 做主体，用户需 1–3 步手动操作 | Spec 显式标注「👤 用户步骤」，验收表单列 |
| 🔴 **射程外** | 当前 skill 无法完成 | P2 提降级方案或砍掉，**禁止偷偷跳过** |

**分级依据必须可查**：源码（C++ / Python 实现）、schema 定义、在线实测——三者至少其一，并写进 P1 报告。

---

## 2 · 七阶段

### P0 · 需求捕获与规格化

| 项 | 内容 |
|:---|:---|
| **输入** | 用户自然语言描述（可含图片 / 参考） |
| **动作** | ① 逐句拆成功能点 R1..Rn；② 每点标注「可见行为」；③ 抽取数值（速度 / 高度 / 半径 / 层数）；④ 定义验收口径（怎么算「做到了」） |
| **输出** | `P0-需求规格书.md`：功能点表 + 数值表 + 验收口径 + **待澄清问题清单** |
| **门禁** | 若存在会改变架构的歧义 → **必须**在 P2 列为拍板点，不得自行假设 |
| **失败处置** | 需求自相矛盾 / 缺关键数值 → 带着「我理解成 A，还是 B？」回去问，一次问清 |

### P1 · 能力预检

| 项 | 内容 |
|:---|:---|
| **输入** | P0 规格书 |
| **动作** | ① 逐功能点对照能力基线（§3）；② 读**源码 / schema**确认字段与事件支持范围；③ 在线只读探测（`ue_send.py` + `hasattr`）确认 API 存在；④ 环境快照（UE 版本 / 项目 / Bridge / 进程） |
| **输出** | `P1-能力预检报告.md`：三色清单 + 每条分级依据 + 环境快照 |
| **门禁** | 🔴 项必须给出降级方案；🟡 项必须写清「用户要做哪几步」 |
| **失败处置** | 环境不通（Bridge 未注入 / UE 未启动）→ 先修环境再预检，不允许「先假设它能用」 |

**P1 只做只读探测**（文件存在性 / API 存在性 / 资产查询），**不创建任何资产**。

### P2 · 方案设计与契约

| 项 | 内容 |
|:---|:---|
| **输入** | P0 规格书 + P1 三色清单 |
| **动作** | ① 架构设计（资产清单 + 组件图 + 数据流 + 楼层/坐标数值）；② 写 **Spec JSON 契约**（字段以源码 schema 为准）；③ 写**验收表**（🤖 自动项 + 👤 人工项）；④ 列**拍板点** |
| **输出** | `P2-执行契约.md` + `spec-<项目>.json` + `acceptance-<项目>.md` |
| **门禁** | Spec JSON 必须可被 `/build` 直接消费（字段名 / 类型 / 嵌套结构逐项核对源码） |
| **失败处置** | Spec 里出现能力基线未覆盖的字段 → 退回 P1 重新分级 |

### P3 · 双签门 🔴

> **本阶段是全流程唯一的硬卡点。未获用户签字（或明确「执行 / 动手 / 做它」），禁止进入 P4。**

| 项 | 内容 |
|:---|:---|
| **输入** | P2 执行契约 |
| **动作** | 向用户呈现：① 射程地图（🟢/🟡/🔴）；② 拍板点（带推荐选项）；③ 预计产物与耗时；④ 风险与回滚方式 |
| **输出** | 用户签字 / 明确授权词 |
| **门禁** | 无授权词 → **停**。用户只回「嗯 / 好 / 看着办」等模糊词 → 复述一次要拍板的具体选项再等 |
| **失败处置** | 用户改需求 → 回 P0，不硬改 Spec |

**双签含义**：用户签「方案可行」+ 用户签「现在可以动手」。二者合一即为一次明确授权。

### P4 · 快照与执行

| 项 | 内容 |
|:---|:---|
| **输入** | 已签字 Spec + 授权 |
| **动作** | ① **首步先做一次能力实测**（把 Spec 里风险最高的字段/节点先跑通一个最小样本）；② 快照现状（资产清单 / 关卡清单）；③ 按 Spec 执行；④ **每步回读**（写完即读，读不通即停） |
| **输出** | 资产 + 关卡实例 + 逐步执行日志 |
| **门禁** | 首步实测失败 → **停**，回报并给降级方案，不允许「继续往下试」 |
| **失败处置** | 同一操作失败 ≥3 次 → 拉会诊（SOUL.md 失败计数器铁律）；执行中出现破坏性风险 → 停手回滚 |

**命名规约**：测试资产一律带日期后缀（如 `BP_XXX_20260909`），主产物用正式名；临时文件执行完立即清理。

### P5 · 自动验收与错误排查

| 项 | 内容 |
|:---|:---|
| **输入** | P4 产物 + 验收表 |
| **动作** | ① 逐项跑 🤖 验收项；② 编译状态 / 回读一致性 / 实例数量与位置 / 节点连线数；③ PIE 启动 + 日志检查；④ 有报错走 5 步诊断（分类 / 快照 / 依赖 / 根因 / 汇报） |
| **输出** | `P5-验收报告.md`：逐项 ✅/❌ + 实测命令与输出 |
| **门禁** | 任何一项「已完成」无实测证据 → 整体视为不完整，退回重测 |
| **失败处置** | ❌ 项 → 修复后重跑该项（不重跑全量）；修复后必须**重新实测该项**，不得凭「应该修好了」勾选 |

### P6 · 交付与实操指导

| 项 | 内容 |
|:---|:---|
| **输入** | P5 验收报告 |
| **动作** | ① 产物清单（路径 + 大小）；② **用户实操测试指南**（👤 人工项逐条写清「按什么键 / 看什么现象 / 预期结果」）；③ 遗留项与后续建议；④ 回滚方式 |
| **输出** | `P6-交付说明.md` |
| **门禁** | 👤 人工项必须可被非专业用户独立执行（不能出现「在蓝图里调一下」这类黑话） |
| **失败处置** | 用户实测不符 → 带现象回 P5 复现，不猜 |

---

## 3 · 能力基线（实测锚点 · 随版本更新）

> 下列结论均来自**源码实读或在线实测**，不是文档推断。基线随 skill 版本演进，每次 P1 需核对是否仍成立。

### 3.1 契约 schema（**以 `game_thread_build.py.tmpl` 实际取值为准**）

| 字段 | 实际读取的键 | 文档写的键 | 结论 |
|:---|:---|:---|:---|
| `blueprint_name` | ✅ 同名 | ✅ | 一致 |
| `package_path` / `parent_class` | ✅ 同名 | ✅ | 一致（`parent_class` 决定蓝图父类） |
| `blueprint_type` | **从未被消费**（全仓 grep 零命中） | 声明在 `BuildRequest` | ⚠️ **摆设字段**，勿依赖它决定类型 |
| `variables[]` | `{name,type,default_value,category,expose}` | 同 | 一致 |
| `components[]` | `{name,component_class,properties}` | runner docstring **未列** | ✅ 真支持（`ue5_bridge.py` L3188-3192 实际遍历） |
| `interfaces[]` | `["IXxx"]` | 同 | 一致 |
| `events[]` | **`event_name`**（主）+ `name`（兼容别名）+ `pos` / `b_override` | `name` / `pos` | ✅ **已对齐**（D-5 · T-20260917-D5D6-CONTRACT-FIX：bridge 归一化 + 模板双层兼容；两者都给且值不同 → 取 `event_name` 并回传 `warnings`；都缺 → fail-loud 点名 `events[i]`） |
| `functions[]` | **`function_name`**（主）+ `name`（兼容别名）+ **`function_path`**（主）/ `target_class`（→ 推导路径）+ `pos` / `defaults` | `name` / `target_class` / … | ✅ **已对齐**（D-8 · T-20260917-D8-FUNCTIONS-CONTRACT：bridge 归一化 + 模板双层兼容；`function_path` 缺省时由 `<target_class>:<函数名>` 推导等价路径并在返回体 `warnings` 标注来源；主字段与别名都给且值不同 → 取 `function_name` 并回传 `warnings`；两者皆缺且无 `function_path` → fail-loud 点名 `functions[i]` + 实收 keys） |
| `connections[]` | `{src_node,src_pin,dst_node,dst_pin}` | 同 | 一致 |
| `compile_after` | ✅ | ✅ | 一致 |

**红线三条**

1. `components` 是真支持字段，不要因为 runner docstring 没写就当它不存在。
2. `events` 推荐写 **`event_name`**（**主字段**）——`name` 是**兼容别名**，两者等价可用；同时给出且值不同时取 `event_name` 并在返回体 `warnings` 告警；两者都缺/为空 → `/build` 直接 fail-loud（错误点名 `events[i]` + 回传实收 keys，不再落到「空事件名 + `add_event_node returned None`」）。`functions` 推荐写 **`function_name`**（**主字段**）——`name` 是**兼容别名**，两者等价可用；`function_path` 为**主字段**（`/Script/模块.类名:函数名`），也可只给 `target_class` + 函数名，由代码推导等价路径（返回体 `warnings` 标注来源=target_class 推导）；同时给出且值不同时取 `function_name` 并告警；两者都缺/为空且无 `function_path` → `/build` 直接 fail-loud（错误点名 `functions[i]` + 回传实收 keys，不再落到「空函数名 + `add_function_node returned None`」）。
3. `create_actor_blueprint` 会**默认创建** `BeginPlay` / `ActorBeginOverlap` / `Tick` 事件，节点标题为**本地化中文**（「事件开始运行」「事件Actor开始重叠」「事件Tick」）。重复添加会导致编译 `Status=2`（「找到了多个同命名为 ReceiveBeginPlay 的函数」）——Spec 里**不要**重复列这三个事件。
4. `components[].properties` 的**值必须可字符串化**（`_component_property_value_to_string`）：`bool`→`"true"/"false"`；数值→`str`；向量 / 旋转用 **list**（如 `[0,0,-90]`，自动拼成 `"0 0 -90"`）；结构体用 **dict**（自动拼成 `"Key=Value ..."`）。⛔ 不要写 `{"x":..,"y":..,"z":..}` 当向量。
5. `functions[].function_path` 格式 = **`/Script/模块.类名:函数名`**（如 `/Script/Engine.KismetSystemLibrary:Delay`），代码按最后一个 `:` 拆成 target_class + function_name（`ue5_bridge.py` L1038-1045）。**可省略**：只给 `function_name` + `target_class` 时由 `_normalize_build_functions` 拼出等价路径（见第 2 条 D-8）。

### 3.2 事件节点能力（C++ `BlueprintPythonBridge.cpp` L257-330 `AddEventNode`）

`AddEventNode` 只在类层级中查找 **`FUNC_BlueprintEvent`** 标志的 `UFunction`（搜索顺序：蓝图类 → `AActor` → `UActorComponent`）。

| 事件 | 可用 | 说明 |
|:---|:--:|:---|
| `ReceiveBeginPlay` / `ReceiveTick` | ✅ | Actor 事件函数名带 `Receive` 前缀 |
| `ReceiveActorBeginOverlap` / `ReceiveActorEndOverlap` | ✅ | 重叠触发 |
| `ReceiveActorOnClicked` | ✅ | 需 PlayerController 开启 click events |
| **输入轴 / 输入动作事件**（`InputAxis MoveForward`、EnhancedInput Action） | ❌ | 它们是专用 K2Node（`UK2Node_InputAxisEvent` 等），**不是 UFunction**，`AddEventNode` 查不到 |

**由此得出的 WASD 通路**：不走输入映射资产，改走 **`ReceiveTick` + `IsInputKeyDown` 轮询**（见 §3.3）。

### 3.3 在线实测 API（2026-09-09 · dev-project / UE 5.1.1 · Bridge 8889）

| API | 存在 | 用途 |
|:---|:--:|:---|
| `unreal.PlayerController.is_input_key_down(key)` | ✅ | WASD 轮询（`-> bool`，注释：Returns true if the given key/button is pressed） |
| `unreal.PlayerController.was_input_key_just_pressed(key)` | ✅ | 单次触发（点击 / 跳跃） |
| `unreal.Actor.add_actor_world_offset` | ✅ | 位移 |
| `unreal.Character` / `unreal.CharacterMovementComponent` | ✅ | 角色父类与移动组件 |
| `unreal.EditorLevelLibrary.spawn_actor_from_class` | ✅ | **编辑器态摆放实例（关卡组装可自动化）** |
| `unreal.EditorActorSubsystem` | ✅ | 同上（UE5 新版入口） |
| `unreal.AssetTools` / `unreal.AssetToolsHelpers` | ✅ | 资产创建 / 复制 |
| `unreal.WidgetBlueprint` / `unreal.WidgetBlueprintFactory` | ✅（资产） | UMG 资产可建，**但 WidgetTree 构建 + 事件绑定仍未验证** → 暂列 🟡 |
| `unreal.InputMappingContext` / `unreal.InputAction` | ✅（资产） | Enhanced Input 资产可建，**但节点绑定不可用** → 列 🟡（无用武之地） |
| 美术网格 / 角色模型资产 | ❌ | 项目内无现成角色 / 电梯模型 → 用引擎基础形状（Cube / Cylinder）替代 |

### 3.4 已知硬墙

| 墙 | 根因 | 绕过方式 |
|:---|:---|:---|
| 输入映射（Project Settings → Input） | 无 API + `AddEventNode` 不支持输入事件 | `ReceiveTick` + `IsInputKeyDown` 轮询（🟢）；或写 `DefaultInput.ini` + 用户手动加节点（🟡） |
| 2D UMG 楼层面板 | WidgetTree 构建 / 按钮事件绑定未打通 | 用 3D 按钮 Actor + `ReceiveActorOnClicked` 替代 |
| 美术模型 / 动画 | 无资产创建 API | 基础形状 + 可选材质 |
| C++ 重编译 | 二进制不可变（8/23 版本已在线验证） | 只改 Agent 侧编排层 |

---

## 4 · 验收标准规范

| 类别 | 前缀 | 执行者 | 内容要求 |
|:---|:---:|:---|:---|
| 自动验收 | 🤖 | skill / maintainer | 资产存在、编译 Status、回读一致性、实例数量与坐标、节点连线数、PIE 日志 |
| 人工验收 | 👤 | 用户 | 键盘 / 鼠标操作 + 视觉现象 + 预期结果（写成非专业用户可照做的话） |

**判定规则**：🤖 全绿 = 交付基线达成；👤 全绿 = 功能达成。二者缺一不算闭环。

---

## 5 · 失败处置与回滚

| 场景 | 处置 |
|:---|:---|
| 单步失败 | 停，5 步诊断，回报现象 + 根因推定 + 降级方案 |
| 同操作失败 ≥3 次 | 强制拉会诊（maintainer + 至少 1 个相关 Agent） |
| 破坏性风险（删资产 / 覆盖文件） | 先快照 + 列清单，用户确认后才动 |
| 需回滚 | 用 P4 快照比对差异，逐项还原；不留半成品 |
| 产物不符预期 | 带现象回 P5 复现，禁止「猜着改」 |

---

## 6 · 交付物规范

```
output\<任务ID>\
  ├─ P0-需求规格书.md
  ├─ P1-能力预检报告.md
  ├─ P2-执行契约.md
  ├─ spec-<项目>.json          # 可被 /build 直接消费
  ├─ acceptance-<项目>.md      # 🤖 + 👤 验收表
  ├─ P5-验收报告.md
  └─ P6-交付说明.md
```

**产物路径必须含端前缀**（`default\` / `manong\` / `qiji\` / `zongguan\`）。

---

## 7 · 角色分工

| 角色 | 职责 |
|:---|:---|
| 用户 | 提需求 / P3 签字 / 👤 人工验收 |
| maintainer | P0–P2 主导、P3 呈现、P5 验收把关、P6 交付 |
| dev（manong） | P4 执行、脚本与 skill 侧修复 |
| QA（qiji） | 方案语义评审、流程复盘 |
| 执剑者（hGmevJ） | 发布前安全审计 |
| rose | 复盘入库 / KB 维护 |

---

## 8 · 命令速查

```bash
# 环境体检
python skills/ue5-automation/scripts/ue5_runner.py health

# 只读探测（UE 内执行任意 Python）
python skills/ue5-automation/scripts/ue_send.py "import unreal; print(hasattr(unreal.Actor,'add_actor_world_offset'))"

# 读蓝图
python skills/ue5-automation/scripts/ue5_runner.py read /Game/Blueprints/BP_X --level L1

# 按 Spec 建蓝图
python skills/ue5-automation/scripts/ue5_runner.py build spec-x.json

# 连线
python skills/ue5-automation/scripts/ue5_runner.py connect /Game/Blueprints/BP_X "节点A" "then" "节点B" "execute"
```

**排错前先看**：`docs/pitfalls.md`、`docs/quality_gates.md`（G1–G5 闸门）。

---

## 9 · 版本与维护

| 版本 | 日期 | 变更 |
|:---|:---|:---|
| v1.0 | 2026-09-09 | 定稿：五步设想扩为 P0–P6 七阶段；补 P1 能力预检（三色清单）、P3 双签门、P5 自动验收；固化能力基线（§3） |

**维护规则**：能力基线（§3）随每次实测更新；新增硬墙必须在 §3.4 登记；本文件随发布包分发，修改需 bump `package_version`。
