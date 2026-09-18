# SOP · ue5-automation Skill 使用标准作业流程

> **面向对象**：把 ue5-automation 部署到**新电脑 / 新宿主机 / 单 Agent 干净环境**并投入日常使用的使用者。
> **文档定位**：**路线图**——只给"先做什么、后做什么、卡了去哪查"，不带实现细节；步骤细节一律指向对应手册。
> **适用版本**：skill v0.15 · init.py v1.1 · role schema v1.1
> **命令前提**：下文所有命令均在 **skill 根目录**（`skill/ue5-automation/`）下执行；UE 编辑器须**已打开目标项目**（写操作需要）。
> **关联文档**：`SKILL.md`（功能使用手册）/ `DEPLOY.md`（Bridge 部署细节·初始化同源完整版）/ `docs/SOP-UE-HTTP-BRIDGE.md`（HTTP Bridge 启动与排查）/ `docs/pitfalls.md`（踩坑库）/ `docs/preflight.md`（30 秒预检）/ `workflow.md`（调度侧分工链）

---

## 0 · 一句话流程

```
① 前置检查（环境/插件/依赖）
② init.py 初始化（探测 → 建议稿 → 人工确认门 → confirmed）
③ 部署验证（health → 读 → 写 三级连通）
④ 日常使用（按模式调用）
⑤ 故障排查（按速查表定位）
```

---

## 1 · 适用场景

| 场景 | 是否走本 SOP |
|:---|:---|
| 新机器首次部署 ue5-automation | ✅ 全流程 |
| 现有机器新增一个 UE 项目 | ✅ 从 ③ 部署验证开始（角色配置通常已生效） |
| 单 Agent 干净环境（≤1 enabled agent） | ✅ 全流程（初始化自动 `all_in_one`，无需人工确认门） |
| 多 Agent 团队环境（当前 default 团队） | ✅ 全流程（初始化产出建议稿，**须人工确认门**） |
| 只是想快速自查环境是否可用 | → 用 `docs/preflight.md`（30 秒版） |

---

## 2 · 前置检查

> 逐项确认后再进初始化，缺项补完再继续。详细命令/口径 → 对应"详见"文档。

| # | 检查项 | 标准 | 详见 |
|:--:|:---|:---|:---|
| 1 | Unreal Engine | 5.1 或更高 | DEPLOY.md §一 |
| 2 | Python Editor Script Plugin | 已启用 | DEPLOY.md §一 |
| 3 | Remote Execution | 已勾选（权威口径：Project Settings → Plugins → Python）| DEPLOY.md §七 步骤 6 |
| 4 | Agent 侧 Python 依赖 | `requests` 可用（Mermaid 渲染需要）| DEPLOY.md §七 步骤 7 |
| 5 | 目录完整 | scripts/ config/ docs/ templates/ 齐全 | DEPLOY.md §十 |
| 6 | Bridge 插件源 | `bridge/` 含 DLL（828,416B）与头文件 | DEPLOY.md §七 |

> ⚠️ 写操作（build/connect/编辑）依赖 **BlueprintPythonBridge 插件**；读操作只需 Remote Execution。如果只做分析/读蓝图，第 6 项可后置。

---

## 3 · 初始化（init.py 薄执行器）

> init.py = **探测 + 产出建议稿 + 人工确认门**。默认只读预览，**不自动生效、不静默覆盖**。
> 三步流程完整语义、判定规则、产物与后续人工步骤 → **DEPLOY.md §五**（同源权威版，本文只给命令骨架）。

### 3.1 第 ① 步 · 探测预览（dry-run，只读零副作用）

```bash
python scripts/init.py
```

→ 输出探测结果（agent 数量/UE 定位）+ 拟分配角色预览。**不落盘**。符合预期再进 ②。

### 3.2 第 ② 步 · 落盘建议稿

```bash
python scripts/init.py --apply
```

→ 产出 `output/role_assignments.yaml`（`status: proposed`，**未生效**）+ `output/init_report.md`。

> ⚠️ **`--apply` 副作用**：若引擎/项目定位成功且插件源 verify 通过，phase3 会**顺带部署 Bridge 插件**（见 §4.1），早于角色稿人工确认。只想预览 → 用 dry-run。

### 3.3 第 ③ 步 · 人工确认门（二选一）

> 🔴 **proposed → confirmed 必须走人工通道**——防静默覆盖的核心闸门。多 Agent 环境必走；单 Agent（≤1 enabled）自动 `all_in_one` 直接 confirmed，无需本步。

```bash
# 方式 A · 显式指派（须覆盖全部 auto 角色，否则仍 proposed）
python scripts/init.py --apply --assign <agent_id>=orchestrator --assign <agent_id>=ue_executor

# 方式 B · 手编建议稿后 reconcile
python scripts/init.py --reconcile --apply
```

### 3.4 判定速查（完整判定表 → DEPLOY.md §五 5.2）

| 场景 | 结果 |
|:---|:---|
| single_agent（≤1 enabled agent） | `confirmed`（all_in_one 自动生效） |
| multi_agent 自动分配 / `--assign` 只派部分角色 | `proposed`（**须人工确认门才生效**） |
| 既有 confirmed + 无人工通道 + role_map 变化 | **拒绝落盘 exit 3**（保护性拒绝）→ 重跑 dry-run 对比变化来源 |

---

## 4 · 部署验证

### 4.1 Bridge 插件部署

- **自动**：`init.py --apply`（§3.2）已顺带部署 → 回看 `output/init_report.md` 中 verify=通过
- **手动**：自动部署未执行或失败时 → **DEPLOY.md §七**（`<Project>/Plugins/BlueprintPythonBridge/` 需含 `.uplugin` + `Binaries/Win64/*.dll` 828,416B）

### 4.2 启动 UE 侧

> ⛔ **弃用配方（2026-09-14 · 缺陷 D-4）**：**不要**用
> `UnrealEditor.exe <uproject> -ExecutePythonScript=<boot.py>` 一键拉起 Bridge。
> - **实测后果**：启动脚本跑完约 **0.5s** 后触发 `QUIT_EDITOR` → 编辑器自杀、进程变僵尸（8889 仍 LISTENING 但请求 403）；且 **Bridge token 每次启动重新生成**（沿用旧 token 直接 403）。
> - 桥内代码 / boot 脚本 / 全局配置 grep **均无**字面 `QUIT_EDITOR` → 判定为**该配方自身触发**，非脚本残留。
> - **正确做法**：① **干净启动** UE（WMI 脱离进程树，**不带** `-ExecutePythonScript`）→ ② 用 `ue_send.py`（UDP 多播发现，不依赖 TCP 6996）**注入** Bridge（见下步骤 4）。
> 详见 `docs/SOP-UE-HTTP-BRIDGE.md` 方式 A/C 与 `pitfalls.md`。

```text
1. 启动 UE 编辑器并打开目标项目
2. Edit → Plugins → 确认 BlueprintPythonBridge 已启用（首次部署后须重启编辑器）
3. Project Settings → Plugins → Python → ☑ Enable Remote Execution（与 §2 一致）
4. 注入 Bridge（Editor 无需焦点 · 编辑器启动完成后执行）：
   python scripts/ue_send.py "
   import sys
   sys.path.insert(0, r'<skill 根目录>\scripts')   # 替换为 ue5-automation 在本机的实际根目录
   import ue5_bridge
   ue5_bridge.start(port=8889)
   "
```

> 💡 日常最省事：直接调 `ue5_runner.ensure_http_bridge()`（方式 A），它在「不活」时会自动走 ue_send 注入。

### 4.3 三级连通测试（从浅到深，逐级通过）

```bash
# ① 健康检查（读通道 · UDP 发现，不依赖固定端口）
python scripts/ue5_runner.py health
# ② 读测试（逆向识别一条已知蓝图）
python scripts/ue5_runner.py read /Game/Test/BP_HealthCheck --level L1
# ③ 写测试（HTTP Bridge :8889 自动 ensure 启动）
python scripts/ue5_runner.py build spec.json
```

| 层级 | 通过标准 | 失败去向 |
|:--:|:---|:---|
| ① health | 返回 UE 在线 + 版本 | `docs/SOP-UE-HTTP-BRIDGE.md` 排查清单 |
| ② read | 返回蓝图结构 JSON | SKILL.md §七 7.1 / FAQ Q2 |
| ③ build | 资产创建成功 | `docs/SOP-UE-HTTP-BRIDGE.md` 踩坑 1-4 |

> ✅ 三级全过 = 部署完成，可进入日常使用。spec.json 字段契约见 `scripts/README.md → ue5_runner.py`。

---

## 5 · 日常使用（模式速查）

> 完整参数、触发词、分工链 → **SKILL.md §三** 与 `workflow.md`。此处只给"该用哪个模式"。

| 你要做什么 | 模式 | 一句话入口 | 通道 |
|:---|:---|:---|:---|
| 读懂一个蓝图 | A · 分析 | `分析蓝图 /Game/.../BP_X` | 读（UDP）|
| 搭一个蓝图骨架 | B · 创建 | `创建一个蓝图用于 <用途>` | 写（HTTP 8889）|
| 查一个报错 | C · 诊断 | 丢日志/报错文本 | 读 + 解析 |
| 评估整个项目 | D · 优化 | `分析项目 <路径>` | 读 |
| 改已有蓝图（改参/断连/重连/删节点）| 增量修改 | `修改蓝图 BP_X：把 <A> 参数改为 <B>` | 写（HTTP 8889）|

**跨模式规则：** 写操作前先 `python scripts/preflight.py <blueprint.json>`（8 项陷阱检查准入）；报告语言与 Mermaid 约定见 SKILL.md §四；调度由 orchestrator 串链（单 Agent = `all_in_one` 自执行）。

---

## 6 · 故障排查速查

> **HTTP 写通道故障 → `docs/SOP-UE-HTTP-BRIDGE.md`**（方式 A-C + 排查清单；现役端口 8889）。

| 现象 | 首选排查文档 |
|:---|:---|
| HTTP 超时 / Connection Refused（写操作失败）| `docs/SOP-UE-HTTP-BRIDGE.md`（方式 A-C + 排查清单）|
| 403 Unauthorized / invalid token | 同上（踩坑 2：Token header 用 `X-Skill-Token`）|
| 创建蓝图无连线 / 参数没生效 | SKILL.md FAQ Q1 + `docs/pitfalls.md` |
| 分析看不到节点详情 | SKILL.md §七 7.1 + FAQ Q2 |
| Python 插件不可用 | SKILL.md §七 7.2 |
| 端口 8889 被占 | SKILL.md FAQ Q3 |
| 初始化 exit 3（拒绝落盘）| 本 SOP §3.4 → 重跑 dry-run 对比 role_map 变化来源 |
| 探测不到 agent / UE 定位失败 | `init_report.md`（exit 3 不刷新此文件）→ DEPLOY.md §五 5.4 |
| 其它已踩过的坑 | `docs/pitfalls.md`（实测踩坑库，持续追加）|

---

## 7 · 附录 · 命令速查卡

```bash
# 初始化（命令骨架 = §3；语义 = DEPLOY.md §五）
python scripts/init.py
python scripts/init.py --apply
python scripts/init.py --apply --assign <id>=orchestrator --assign <id>=ue_executor
python scripts/init.py --reconcile --apply
python scripts/init.py --deploy-script <path>
python scripts/init.py --api-base <url>

# 部署验证（§4.3）
python scripts/ue5_runner.py health
python scripts/ue5_runner.py read /Game/Path/BP_X --level L2
python scripts/ue5_runner.py build spec.json
python scripts/ue5_runner.py connect <bp> <src> <pin> <dst> <pin>
python scripts/preflight.py <blueprint.json>

# 发布（开发侧 · 仅 skill 维护者使用）
python scripts/publish.py                     # 重发部署包（docs 白名单自动含本 SOP；版本自动 +0.1）
python scripts/publish.py --package-version vX.Y   # 或显式指定部署包版本
python scripts/publish.py --with-tests        # 附测试基建（默认不带）
```

---

*本文档由 T-20260909-SOP-FORM 起草 · DOCFIX（T-20260909-SOP-FORM-DOCFIX）修订 · 结构选项 1 导航化瘦身（2026-09-09 用户拍板，实现细节统一指向 DEPLOY.md §五/§七）· 2026-09-09 终审通过定稿（v2.4 随包发布）。*
