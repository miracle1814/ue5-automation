# UE5 蓝图自动化（SkillHub 包）

这是 `ue5-automation` 的 SkillHub 轻量技能包，只包含**操作规范与参考文档**；
运行包（UE 插件 + bridge）单独分发。

## 包内容

| 文件 | 用途 |
|---|---|
| `SKILL.md` | 技能入口：定位、工作流、硬规则、安全边界 |
| `references/INSTALL-WINDOWS.md` | 安装、连接、客户端配置、自检 |
| `references/COMMANDS.md` | 按任务分组的命令目录 |
| `references/EXAMPLES.md` | 读图 / 写图 / 修编译 / 材质 / UMG / DataTable / 撤销 / 多 Agent |
| `references/PITFALLS.md` | 已知陷阱 Top 20+（症状 → 根因 → 修复） |
| `references/TROUBLESHOOTING.md` | 常见故障排查 |
| `references/SECURITY.md` | 安全模型与授权边界 |
| `references/VERIFICATION.md` | 验证记录与已知边界 |
| `scripts/bridge_client.py` | doctor / smoke 共用：health 归一化、重试、结构化错误 |
| `scripts/doctor.py` | 安装后体检：token / health / bridge 版本 / 项目路径 |
| `scripts/smoke.py` | 5 分钟主路径冒烟（会创建/替换 `/Game/Test/BP_Smoke`） |

## 当前版本

| 项 | 版本 |
|---|---|
| Skill 包 | **6.5.12** |
| 运行包（插件 + bridge） | **6.5.5** |
| 适用引擎 | UE 5.1.x 开箱；5.8.x 预编译；5.2–5.7 需重编译 |
| 更新日期 | 2026-11-03 |

## 版本时间线

| 日期 | Skill | 运行包 | 主要变化 |
|---|---|---|---|
| 2026-10-29 | 6.5.9 | 6.5.4 | UE 5.1.1 实机冒烟后的文档修订 |
| 2026-11-02 | 6.5.10 | 6.5.5 | 节点搜索、启动幂等、doctor/smoke |
| 2026-11-03 | 6.5.11 | 6.5.5 | 自检脚本可靠性：统一 health、重试、结构化错误 |
| 2026-11-03 | 6.5.12 | 6.5.5 | 文档一致性与调用路径整理 |

## 6.5.12 改了什么

- 统一陷阱编号：示例中的 `PTN-010` 改为与 `PITFALLS.md` 一致的 `P10`。
- 工具/命令数量只从 `health.mcp_tool_count` 读取；删除文档中的重复硬编码。
- 明确“每个任务只选一种调用路径”：MCP 工具或 HTTP command，不混排。
- 新增材质 / UMG / DataTable 的最小路径，补齐这三类触发场景。
- 版本与日期集中到“当前版本 + 版本时间线”，删除重复历史口径。
- 运行包仍为 6.5.5，本轮不改 bridge/插件/DLL。

## 6.5.11 改了什么

- 新增 `scripts/bridge_client.py`：同时兼容 `/health` 的嵌套 `ue_info` 与
  `/command health` 的顶层字段，doctor / smoke 不再各写一套解析。
- 自检脚本默认 `--retries 2`：仅对连接失败和 HTTP 5xx 重试；401/403 立即失败。
- 失败输出统一为 `code` / `message` / `hint` / `http_status`，`--json` 同样包含。
- 验证记录区分本轮实测与历史数据。

## 6.5.10 改了什么

- 运行包 6.5.5：`search_blueprint_nodes` 内置 `PrintString` / `Delay` 目录，
  `Print` 不再命中一堆 `Blueprint*`；`BeginPlay` 可命中 `ReceiveBeginPlay`。
- 运行包 6.5.5：`start()` 增加跨模块启动锁与 5 秒端口宽限，消除 10048 双自启。
- 运行包 6.5.5：PIE 世界 `spawn_actor` 在 UE 5.1 无生成入口时明确报错。
- Skill 6.5.10：新增 `scripts/doctor.py` 与 `scripts/smoke.py`，smoke 可重复运行。

## 6.5.9 改了什么

- 补充 UE 5.1.1 实机冒烟结论、`read_blueprint` L2 结构、bool 断言与
  `task_begin` 时机；修正安装脚本路径和引擎 Python 依赖安装方式。

> 更早的 6.5.8 及以前历史见仓库 `CHANGELOG.md`，本包不再重复堆叠旧版本说明。

## 安装

见 `references/INSTALL-WINDOWS.md`。最短路径：

1. 下载与运行包版本匹配的 portable 包：
   `https://github.com/miracle1814/ue5-automation/releases/tag/v6.5.5`
2. 解压后运行 `install.bat`，把项目 `.uproject` 拖进窗口。
3. 重启 UE 编辑器，Output Log 出现 `[ue5_bridge] [OK] 服务已在后台启动`。
4. MCP 连接 `http://127.0.0.1:8889/mcp`，请求头带 `X-Skill-Token`。
5. 调 `health`，确认 `bridge_loaded=true`。

## 企业合作 / Commercial

- **企业集成**：把 UE 蓝图自动化接进团队现有 Agent / CI / 资产管线。
- **版本适配**：UE 5.2–5.7 的 bridge 重编译与回归。
- **定制开发**：领域模板、批量迁移、审计/审批、私有工具链。
- **培训/支持**：团队上手、故障排查、交付验收。

联系方式：GitHub Issue 标题注明 `[Commercial]`，或通过
https://github.com/miracle1814 联系作者。

## 授权与商标

- 本项目原创代码：Apache-2.0；第三方组件与完整声明见仓库
  `THIRD_PARTY_NOTICES.md`。
- 截图/PIE 部分实现参考 UEBox / UnrealAgentLink（MIT），完整 MIT 文本随仓库与
  移植包分发。
- Unreal、Unreal Engine、UE 是 Epic Games, Inc. 的商标；本 Skill 与 Epic Games
  无隶属或背书关系。引擎头文件/二进制仍受 Epic EULA 约束。
- 本 Skill 不包含 Epic 美术资源、Logo、示例资产或市场内容。

## 相关链接

- 项目主页：https://github.com/miracle1814/ue5-automation
- 运行包发布：https://github.com/miracle1814/ue5-automation/releases
- 问题反馈：https://github.com/miracle1814/ue5-automation/issues
