# UE5 蓝图自动化（SkillHub 包）

这是 `ue5-automation` 的 SkillHub 轻量技能包，只包含**操作规范与参考文档**；
运行包（UE 插件 + bridge）单独分发。

## 包内容

| 文件 | 用途 |
|---|---|
| `SKILL.md` | 技能入口：定位、工作流、硬规则、安全边界 |
| `references/INSTALL-WINDOWS.md` | 安装、连接、客户端配置、自检 |
| `references/COMMANDS.md` | 按任务分组的命令目录 |
| `references/EXAMPLES.md` | 读图 / 写图 / 修编译 / 场景验证 / 撤销 / 多 Agent 示例 |
| `references/PITFALLS.md` | 已知陷阱 Top 20+（症状 → 根因 → 修复） |
| `references/TROUBLESHOOTING.md` | 常见故障排查 |
| `references/SECURITY.md` | 安全模型与授权边界 |
| `references/VERIFICATION.md` | 验证记录与已知边界 |

## 版本对应

| 项 | 版本 |
|---|---|
| Skill 包 | **6.5.7** |
| 运行包（插件 + bridge） | **6.5.4** |
| 适用引擎 | UE 5.1.x 开箱；5.8.x 预编译；5.2–5.7 需重编译 |

> 6.5.7 是**曝光与可发现性修订**，不改变运行时行为。安装、命令、安全模型
> 与 6.5.4 完全一致；`VERIFICATION.md` 中的真机记录沿用 6.5.4。

## 6.5.7 改了什么

- 描述补充 `虚幻引擎` / `Unreal Engine 5` / `Blueprint Automation` 关键词，提升站内搜索命中。
- 默认概述顶部新增工作流总览图（读 → 写 → 编译 → 验证 → 收尾）。
- 新增 `unreal-engine-5`、`ue5-blueprint`、`虚幻引擎` 标签。
- 分类增加 `dev-bug-fix`（编译诊断），与代码生成、脚本工具并列。
- 6.5.6 的元数据修正保留：描述与 frontmatter 明确「本地 bridge 使用本机 token，无需外部 API Key」，
  并声明 `requires_api_key: false`。
- 新增「何时不要用」：明确排除 C++ 代码生成、打包发布、纯美术制作、无 bridge 环境。
- 新增「工具档位」：gateway 11 / core 18 / full 33，附 `mcp_tool_profile` 检查方法。
- 新增 `references/PITFALLS.md`：把 19 条蓝图陷阱与关键构建/环境陷阱整理为可执行速查。
- 新增 `references/EXAMPLES.md`：6 个可直接照抄的端到端示例。
- 命令目录不再写死数量，改为以 `health.mcp_tool_count` / `tool_search` 为准。
- `INSTALL-WINDOWS.md` 移除“如果没有发布包”的退路话术，补客户端配置与 5.8 自启说明。
- 商务合作、许可与商标声明从 `SKILL.md` 移到本 README，技能入口只保留操作内容。
- 统一版本口径：删除 `skill v1.14.0` / `skill v2.0.1` 两套内部编号。

## 安装

见 `references/INSTALL-WINDOWS.md`。最短路径：

1. 下载与运行包版本匹配的 portable 包：
   `https://github.com/miracle1814/ue5-automation/releases/tag/v6.5.4`
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
