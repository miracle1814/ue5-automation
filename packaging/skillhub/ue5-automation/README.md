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
| `scripts/doctor.py` | 安装后体检：token / health / bridge 版本 / 项目路径 |
| `scripts/smoke.py` | 5 分钟主路径冒烟（会创建/替换 `/Game/Test/BP_Smoke`） |

## 版本对应

| 项 | 版本 |
|---|---|
| Skill 包 | **6.5.10** |
| 运行包（插件 + bridge） | **6.5.5** |
| 适用引擎 | UE 5.1.x 开箱；5.8.x 预编译；5.2–5.7 需重编译 |

> 6.5.10 对应运行包 6.5.5：修复节点搜索的 `KismetSystemLibrary` 目录、
> `BeginPlay` 友好名匹配与 `Print` 噪声，修复重复启动的 10048 端口占用，
> 并新增 `doctor.py` / `smoke.py` 安装后自检。

## 6.5.10 改了什么

- 运行包 6.5.5：`search_blueprint_nodes` 内置 `PrintString` / `Delay` 目录，
  `Print` 不再命中一堆 `Blueprint*`；`BeginPlay` 可正确命中 `ReceiveBeginPlay`。
- 运行包 6.5.5：`start()` 增加跨模块启动锁与 5 秒端口宽限，重复加载不再刷
  10048 双自启；锁异常时释放，允许下次重试。
- 运行包 6.5.5：PIE 世界 `spawn_actor` 在 UE 5.1 的 Python API 未导出生成接口时
  明确报错并给出替代路径，不再静默返回 `None`。
- Skill 6.5.10：新增 `scripts/doctor.py` 与 `scripts/smoke.py`，只依赖标准库；
  `smoke.py` 可重复运行（已存在 `/Game/Test/BP_Smoke` 时删除重建）。
- 实测：UE 5.1.1 + `TP_BlankBP`，doctor 全绿；smoke 连续两次全绿
  （`verified=true`、`compiled=true`、读回 `Hello from skill smoke`、`saved=true`）。
- 6.5.9 的文档修订与 6.5.8/6.5.7/6.5.6/6.5.5 的结构改动保留。

## 6.5.9 改了什么

- 按 UE 5.1.1 实机冒烟结果补充：`search_blueprint_nodes` 对图内/事件可靠，
  但 `KismetSystemLibrary` 的 `PrintString` 可能搜不到；示例改为 `BeginPlay`，
  并说明直接用完整类路径 + `read_nodes` 回读。
- 写明 `read_blueprint` L2 返回结构：`result.l2.graphs[].nodes` / `.connections`。
- 写明 `verify_behavior` 的 bool 用小写 `true` / `false`，`exact` 大小写敏感。
- 写明 `task_begin` 要在动手前调用；否则 `task_end` 会因无证据标 `unverified`。
- TROUBLESHOOTING 新增：10048 双自启、搜索限制、bool 断言。
- 安装文档修正：v6.5.4 包的安装脚本在 `tools\install.bat`；引擎 Python 依赖
  必须装到引擎 site-packages，不能用 `--user`。
- VERIFICATION 新增 UE 5.1.1 实机冒烟记录（建图/编译/读回/保存/PIE 验证）。
- 6.5.8 的档位与参数修正保留：gateway **12** / core **18** / full **34**、
  gateway 补 `ue5_verify_behavior`、pin 示例补 `bp_path`、disconnect/delete 命名。
- 6.5.7/6.5.6/6.5.5 的曝光与结构改动保留。

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
