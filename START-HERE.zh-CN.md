# 快速开始（中文）· UE5 Automation Skill

> English quick start: see [README.md](README.md).


> **这是什么**：一套让 AI（大模型/Agent）**自动化操作 UE 编辑器**的工具包 —— 读蓝图、写蓝图、改蓝图、建材质、建 UMG 控件蓝图、跑 PIE 验证行为、诊断报错、评估项目。
> **支持版本**：UE **5.1.x 开箱即用**（随包预编译桥）；5.2–5.8 见下文「版本矩阵」。
> **接入方式**：任意 **MCP 客户端**（ZCode / Claude Desktop / Cursor …）直连，或用脚本/CLI 调用。

---

## 一、30 秒理解架构

```
用户需求 ──▶ 大模型（MCP 客户端 / Agent）──▶ 本工具包 ──▶ UE 编辑器
                                              │
                        ┌─────────────────────┼─────────────────────┐
                        ▼                     ▼                     ▼
                  读通道(UDP 远程执行)   写通道(HTTP 127.0.0.1:8889)   MCP 端点(/mcp)
                  健康检查/轻量读取      蓝图/材质/UMG/PIE/Actor     同一能力的标准协议面
                        └──────────┬──────────┘
                                   ▼
                        C++ 桥（59+5 个 API：节点级读写/控件树/材质枚举）
```

- **没有 C++ 桥**：能做健康检查、项目评估、日志诊断、蓝图读取（部分）。
- **有 C++ 桥**：解锁**节点级**写能力（建事件/函数节点、连线、改引脚默认值、GUID 精确定位、控件树、材质表达式枚举）——这是本工具的核心价值。

## 二、部署（3 步 + 1 验证）

```cmd
:: ① 安装依赖（自动尝试部署桥 DLL）
install_dependencies.bat

:: ② 若①未成功：手动部署桥到你的 UE 项目（推荐项目级，无需管理员）
cd bridge/BlueprintPythonBridge
python deploy.py --target "你的UE项目路径"

:: ③ UE 侧配置：Edit → Plugins 启用 "Python Editor Script Plugin"；
::    Edit → Project Settings → Plugins → Python → 勾选 Enable Remote Execution → 重启编辑器

:: ④ 验证（在 skill/ue5-automation/scripts 下）
python check_ue_version.py        :: 版本自检（0=5.1 可用）
python ue5_runner.py health       :: 链路健康检查（看到 bridge_apis 列表即通）
```

## 三、接入 AI（二选一或都配）

### A. MCP 客户端（推荐，AI 原生体验）

ZCode：`%USERPROFILE%\.zcode\cli\config.json` 的 `mcp.servers` 加：

```json
"ue5-automation": {
  "type": "http",
  "url": "http://127.0.0.1:8889/mcp",
  "headers": { "X-Skill-Token": "<%APPDATA%\\ue5_bridge\\token 文件内容>" }
}
```

连上后 AI 获得 **23 个 MCP 工具**（含 `ue5_build_blueprint` / `ue5_start_pie` / `ue5_read_logs` / `ue5_run_batch` / `ue5_execute_command` 全量透传）+ 72 条白名单命令。
> 详细配置（Claude 等其他客户端 / 无反代客户端用 `?token=` 兜底）见 `skill/ue5-automation/docs/MCP-ACCESS.md`。

### B. 脚本 / 命令行

```cmd
cd skill/ue5-automation/scripts
python ue5_runner.py health                       :: 健康检查
python ue5_runner.py read /Game/BP_HelloMirror    :: 读蓝图
python ue5_runner.py build spec.json              :: 按 JSON spec 创建蓝图
python blueprint_editor.py update /Game/BP_X 打印字符串 InString "Hello"
```

## 四、标准执行链（SOP：需求 → 拆解 → 执行 → 交付）

任何一次自动化任务都按这条链跑（AI 会自动做，这里是给你审计用的）：

| # | 阶段 | 工具动作 | 判据 |
|:--|:--|:--|:--|
| 1 | **需求分析** | 大模型理解用户描述（"做一个可拾取物品/做一个 HUD/查这个报错"） | 输出方案步骤 |
| 2 | **环境预检** | `ue5_check_health` → `check_ue_version.py`；写操作前 `ensure_http_bridge` | bridge_apis 非空 |
| 3 | **读取现状** | `ue5_read_blueprint` / `ue5_read_nodes`（GUID/标题） | 拿到目标节点清单 |
| 4 | **执行变更** | `ue5_build_blueprint`（从零）或 `ue5_execute_command` 系列（增量：连线/改参/删节点/建材质/建 UMG） | 每步 fail-loud 返回 |
| 5 | **行为验证** | `ue5_start_pie` → `ue5_read_pie_state` → `ue5_list_pie_actors`/`ue5_read_pie_property`（读运行时状态；需要位移时 `ue5_set_pie_transform` 带**回读比对**）→ `ue5_stop_pie` | 运行时属性符合预期 = 真验证 |
| 6 | **交付产出** | 分析/创建/诊断/架构报告（Markdown + Mermaid）；事务日志 `logs/blueprint_editor.jsonl` | 报告落盘 |
| 7 | **清理** | `ue5_delete_asset`（保存作用域=引用者包，绝不全工程保存） | 删后回读不存在 |

> **为什么第 5 步重要**：编译通过 ≠ 逻辑正确。工具坚持"读运行时状态证明行为"，并对每个写操作做**回读比对**（比如传送后坐标不对会直接报错，绝不谎报成功）。

## 五、能力清单（v3.0 · 72 条命令 + 23 个 MCP 工具）

| 能力域 | 能做什么 | 代表命令 / 工具 |
|:--|:--|:--|
| 🔍 蓝图分析 | 类信息/变量/CDO/节点/连线 → Mermaid 图 + 报告 | `read_blueprint` `read_nodes` `read_connections` |
| 🏗️ 蓝图创建 | 资产+变量+组件+接口+事件+函数节点+连线+编译（原子化，失败回滚） | `ue5_build_blueprint` / `build-batch` |
| ✏️ 增量修改 | 改引脚默认值 / 连线（三重验证）/ 断线 / 删节点（引用检查）/ 事务回滚 | `connect_pins` `disconnect` `delete_node` + `rollback()` |
| 🎨 材质 | 建材质→加表达式（参数/运算/采样）→连主属性→编译→建实例改参 | `material_*` 9 条 |
| 🧩 UMG 控件蓝图 | 建控件蓝图→搭控件树（Panel/Text/Button…）→设属性→读树→图内逻辑复用蓝图能力 | `widget_*` 4 条 |
| ▶️ PIE 运行时验证 | 启动/状态/运行时对象枚举/属性读取/传送回读/停止 | PIE 6 个工具（`ue5_start_pie` · `ue5_read_pie_state` · `ue5_read_pie_property` · `ue5_set_pie_transform` 等） |
| 🖥️ Actor / 关卡 | 查询、生成（可选 mobility）、变换、读属性 | `find_actors` `spawn_actor` `set_actor_transform` `get_actor_properties` |
| 🩺 报错诊断 | 日志解析 + 19 条错误模式库 + 自取日志 | `logs_read` + `log_parser.py` |
| 📐 项目评估 | 全项目扫描：数量/复杂度/耦合/循环依赖/热力图 | `analyzer.py` |
| ⚡ 批量执行 | 多条命令单次执行（多步任务压成一次往返） | `ue5_run_batch` / `editor_batch` |

## 六、版本矩阵（如实声明）

| 引擎 | 读/评估/诊断 | 蓝图节点级写 | 材质/UMG | MCP 接入 |
|:--|:--|:--|:--|:--|
| **5.1.x** | ✅ 随包即用 | ✅ 随包预编译桥 | ✅ | ✅ `/mcp` |
| 5.2–5.5 | ✅ | ⚠️ **需自助重编译桥**（源码随包）：`cd bridge/BlueprintPythonBridge\Source` → `build_for_engine.bat "你的引擎目录"` | ⚠️ 同左 | ✅ `/mcp` |
| 5.6–5.7 | ✅ | ❔ 未验证 | ❔ | ✅ |
| **5.8** | ✅ | ✅ **随包预编译桥**（`bridge/prebuilt-5.8/`，已实测 5.8.2） | ✅ | ✅ 官方 MCP + `bridge/ue58-extras/`（8 工具；桥 88 方法实测可见） |

> **5.8 说明**：UE 5.8 自带官方 MCP 插件，装 `bridge/ue58-extras/` 即可获得 PIE 状态/日志/Actor/编译工具；**节点级图工具**用随包预编译桥即插即用（`bridge/prebuilt-5.8/`，实测 5.8.2）。如需改 C++ 自编译：5.8 的 UBT 要求 **MSVC ≥ 14.44**（14.40–14.43 被 5.8 UBT 封禁；实测组合 = MSVC 14.44.35207 + VS2022 ≥17.14），编译是**一次性环境准备**，不属于每次任务的执行链。

## 七、常见问题速查

| 现象 | 处理 |
|:--|:--|
| 调用返回 403 | token 不对：读 `%APPDATA%\ue5_bridge\token`；**没有默认 test123** |
| `bridge_apis: []` | 桥未加载：确认 DLL 已部署 + 重启编辑器 |
| ue_send 找不到节点 | 编辑器未开 / Remote Execution 未勾选 / 弹了模态对话框（如崩溃恢复框，点“跳过恢复”） |
| PIE 里物体不动 | 组件是 Static mobility：`spawn_actor` 时传 `mobility="movable"` |
| 编译报 Win32Exception 998 | UBT 并行/内存限流问题：见 `bridge/BlueprintPythonBridge/Source/BUILD.md` 排障表 |
| 更多 | `skill/ue5-automation/SKILL.md`（完整手册）· `docs/pitfalls.md`（19 个已知坑）· `docs/MCP-ACCESS.md`（MCP 接入） |

---

*本文件夹可直接分享传播（不含任何开发机路径/内部信息）。验证报告在 `docs\`。*
