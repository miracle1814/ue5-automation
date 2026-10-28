# Changelog (中文精简版)

以下为主要变化摘要。

## [6.5.4] — 2026-10-28（合规补丁 + 发布同步）

- **合规补齐**：移植包根目录随附 `LICENSE`（Apache-2.0）与
  `THIRD_PARTY_NOTICES.md`；补充 UEBox / UnrealAgentLink 的完整 MIT 文本、
  Unreal Engine 商标与 Epic EULA 边界声明。
- **CI 修复**：JUnit 输出白名单改用 `realpath`，并纳入 `TEMP/TMP/TMPDIR/RUNNER_TEMP`；
  修复 GitHub Runner 临时目录被误判越界。main CI 已绿。
- **发布文档同步**：README / 中文快速开始 / SkillHub 说明统一到 v6.5.4；
  Release 下载直链、SkillHub 入口、101 命令 / 34 工具 / 820 测试口径一致。
- **公开文档分层**：新增“5 分钟最小成功路径”“Top 10 故障排查”“企业合作”入口；
  竞品/路线图类策略文档移入 `docs/private/`，不再随公开仓库和移植包分发。
- 运行包重打包，确保公开资产与最新代码一致。

## [6.5.0] — 2026-10-28（合并「已优化版本A」：多 Agent + 稳定性 · skill v1.14.0）

来源：`已优化版本A/UETool-v6.2.1-完整交付.zip`（UETool v6.2.1-dev，基线 v6.0.2）。
逐项合并，未整包覆盖；完整矩阵见 `skill/ue5-automation/docs/MERGE-v6.5-已优化版本A.md`。

- **幂等键**（`idempotency_key`）：10 分钟内同 key+同 command/params 重放成功结果；
  跨命令复用 `idempotency_conflict` fail-loud；失败结果不缓存。
- **显式会话锁**：`session_lock_acquire/release/status`；与 v6.3 任务锁互相可见；
  `force` 只抢会话锁，不抢任务锁；TTL/数量/NaN 全部有上限。
- **回执查询**：`receipt_list` 最近 200 条；回执新增 `receipt_id` /
  `idempotent_replay`，保留 before/after sha1。
- **AnimBP 只读**：`animbp_probe` / `animbp_read`；读不到 fail-loud，写入未开放。
- **场景安全**：`command` / `pie_start` / `pie_stop` 步骤、`action` 别名、
  高危命令默认拒绝、总超时（默认 300s，上限 1800s）、禁止递归/批量绕过。
- **HTTP 体积上限** 8MB（`UE5_MAX_REQUEST_BYTES`）；非法 Content-Length → 400。
- **stdio 去 requests**：标准库 urllib；token 不再回退 `test123`，桥启动后重读 token。
- **工具面**：命令 95→101，工具仍 34；gateway 5→12，新增 `UE5_MCP_PROFILE`。
- **发布自检**：`tools/verify_release.py`、`tools/check_tool_profile.py`、CI 工作流。
- **测试隔离修复**：E-11 token 测试曾直接覆盖真实 `%APPDATA%/ue5_bridge/token`
  → 改为 `UE5_BRIDGE_TOKEN_FILE` 临时路径；真实 token 已轮换。
- 测试：+17（12 多 Agent/HTTP/AnimBP/token + 5 stdio urllib）+ 场景 6；
  **820 passed / 95 skipped / 0 failed**。
- 真机：UE 5.1 + 5.8 全主路径回归（见 `docs/CROSS-VALIDATION-v6.5.md`）。

## [6.4.1] — 2026-10-28（收尾硬化：token 不进 URL + ReDoS 预检 · skill v1.13.1）

- **token 不进 API URL**：面板数据请求改用 `X-Skill-Token` 请求头；
  打开 `/ui` 不再需要把 token 放进地址栏（截图 img 仍用查询参数，浏览器限制）。
- **日志正则 ReDoS 预检**：`logs_read` 拒绝嵌套量词形态（`(a+)+` / `(.*)*` 等），
  保留 200 字符上限；场景日志断言仍为字面量。
- 测试：+2（头认证契约 / ReDoS 拒绝）；**全量 797 passed / 0 failed**。
- 真机：无 token URL 打开面板 → 8 条任务/已连接/无 401 横幅。

## [6.4.0] — 2026-10-28（安全硬化 + 稳定性回归 · skill v1.13.0）

不新增功能，只排漏洞/修 bug（完整审计见 `docs/SECURITY-AUDIT-v6.4.md`）：

- **路径逃逸修复**：资产路径统一 `_safe_game_pkg`（拒绝 `..`/`.`/反斜杠/非 `/Game/`），
  覆盖哈希、快照、批量、DataTable、创建与保存；`delete_asset`/`save_asset` 限定 `/Game/`。
- **任意文件读取修复**：`logs_read.log_file` 只允许项目日志 / `Saved/Logs` / 系统临时目录。
- **任意文件写入修复**：`verify_scenario.junit_path` 输出白名单（项目 Saved / APPDATA / Temp）。
- **DoS 收紧**：CSV 5MB 上限；日志 pattern ≤200 字符；场景日志断言改字面量（防 ReDoS）。
- **锁修复**：锁只由任务持有（adhoc 不加锁）；批量操作对每个 from/to 加锁；
  `task_end` 异常也释放；`execute_command` 透传的场景走 HTTP 线程。
- 测试：新增安全测试 10 项；**795 passed / 0 failed**。
- 真机 5.1 + 5.8：路径/日志/JUnit 越界全部被拒，正常读写与 34 工具目录不受影响。

## [6.3.0] — 2026-10-28（变更回执 + 多 Agent 资产锁 + 面板可见 · skill v1.12.0）

补齐竞品报告里 EngineWire/Claireon 的两项治理能力：

- **变更回执（receipts）**：每次写操作返回 `receipt`（before/after 文件 sha1、size、
  `saved`、`verified`、变更计数）；随任务账本持久化；面板交付物区显示最近 12 条。
  覆盖写图/建资产/删除/保存/批量/DataTable/材质/UMG 等 20+ 写命令。
- **多 Agent 资产锁**：同一资产同时只允许一个任务写；owner=task_id，
  无任务时单命令租约；`task_end`（含异常）释放，TTL 300s 可调
  （`UE5_ASSET_LOCK_TTL`）；冲突 fail-loud；面板「运行状况 → 资产锁」可见。
- 真机发现并修复：adhoc 锁曾不释放导致后续任务被阻塞 → 改为单命令租约。
- 管理页：交付物区新增“变更回执（before → after 指纹）”；运行状况新增“资产锁”。
- 测试：新增 6 项（回执指纹/saved/锁冲突/任务释放/adhoc 租约/账本上限）；
  **全量 785 passed / 0 failed**（以实测为准）。
- 真机 5.1：3 条回执（build/写/保存，保存 saved=true）、任务持锁→task_end 释放。

## [6.2.0] — 2026-10-28（差距分析 + 独家场景验收 + 管理页再设计 · skill v1.11.0）

对照外部竞品报告逐条列差距（内部规划文档，不公开），
只做能形成组合优势的升级：

- **独家：`verify_scenario` 场景化验收**：PIE 自动起停 + wait/assert/screenshot/
  log_contains 步骤 + **JUnit XML**；面板“运行状况 → 验收场景”直接看到通过/失败。
  与 `apply_graph`/`compile_and_diagnose`/`task_end`/`agent_undo`/资产回滚串成
  「写入→编译→行为→证据→可撤销」完整链——竞品少有同时具备。
- MCP 工具 33→**34**、白名单 94→**95**；场景命令必须在 HTTP 线程跑（GT 内调用报错）。
- **管理页再设计**：编辑级极简（单色 + 发丝线 + 无阴影 + 等宽数字）；阶段轨道改为
  极简步骤器；去全部 emoji（线性 SVG 图标/序号）；筛选计数、KPI 语境、空卡片收紧、
  三档响应式、长项目名省略。
- 测试：新增场景 5 项；**全量 779 passed / 0 failed**（以实际为准）。
- 真机 5.1：场景通过（4 步 + JUnit 落盘）/ 故意失败（failures=1）/ 面板两条记录。

## [6.1.0] — 2026-10-28（竞品报告落地 + 管理页重设计 · skill v1.10.0）

依据外部竞品报告与执行方案（内部文档，不公开），
只采纳不违背既定边界的条目：

- **上下文网关（opt-in）**：`UE5_MCP_GATEWAY=1` → 5 个入口；默认仍全量 **33** 工具，
  `UE5_MCP_CORE_TOOLS=1` → 18。新增 `ue5_verify_behavior` 专用 MCP 工具。
- **读通道安全默认收紧**：`ue_send.py` 的 UDP Remote Execution 默认关闭，
  `UE5_ALLOW_REMOTE_EXEC=1` 显式开启（同网段多播无鉴权）；日常读走 HTTP。
- **公开基准**：`benchmark/run_offline.py`、`benchmark/live_smoke.py` + README，
  一键复现测试与真机体检。
- **英文入口**：根级 `README.en.md`（定位/能力/Quickstart/安全/基准）。
- **管理页重设计**：编辑级极简单色主题（发丝线、无阴影、等宽数字、线性图标），
  KPI 去彩色/筛选带计数/空卡片收紧/三档响应式；去全部 emoji 装饰。
- 测试：新增读通道守卫 + 网关档；**全量 774 passed / 0 failed**。

## [6.0.3] — 2026-10-28（管理页可读性 + 测试隔离）

- **任务筛选带计数**：全部/进行中/失败/成功 显示条数，切换前先知道有多少。
- **KPI 带语境**：总任务数（已结束 N）/ 成功率（6/8）/ 累计耗时（均 Xs），
  不再只有孤立数字。
- **空段落收紧**：原话/逻辑/步骤为空的卡片减少内边距，长任务不再被占位撑高。
- **窄窗口适配**：≤1180/1000/760 三档（KPI 2×2、阶段轨道折行、前后对照单列）。
- **修测试污染**：`task_journal` 在 TEST_MODE 下写临时目录 —— pytest 不再向真实
  账本写 "g"/"检查门…" 噪声（这是清理后又冒出新记录的根因）。
- 账本清理工具已用于生产：11 → 8 条（备份保留）。
- 测试：+1（测试不写生产账本）；**772 passed / 0 failed**。

## [6.0.2] — 2026-10-28（账本整理 · 面板收尾）

- **任务账本清理**：本机 70 条历史 → **8 条有效任务**（备份在
  `%APPDATA%/ue5_bridge/tasks-backup-<时间>/`）；新工具 `scripts/task_cleanup.py`
  （默认 dry-run，`--apply` 备份后重写，自动重新编号）。
- **回放去重 + 上限 50**：旧账本同 (id, started_at) 不再重复入内存；
  面板不会随时间无限堆积（内存与显示都收敛）。
- 任务项 meta 单行省略（证据文字不再掉到第二行）。
- 测试：+4（清理规则/去重/重编号/dry-run 不改盘）；**771 passed / 0 failed**。

## [6.0.1] — 2026-10-28（面板修复：任务列表颜色/点击 + token 接入）

用户反馈"任务列表颜色深浅不一、点击没反应"——根因是两个真 bug：

- **历史任务 ID 重复**（同日多次重启的旧账本，`20261028-01` 有 96 条）→ 27 个
  条目同时带 `sel`，看起来颜色深浅不一；点击按 ID 找到的永远是第一条。
  · 账本：`task_begin` 增加唯一性守卫（撞历史 ID 自动顺延）；
  · 面板：选择键改为 `task_id + started_at`，历史重复数据也能正确区分。
- **点击被 1s 轮询的 DOM 重建吞掉**：任务列表改为容器事件委托 + 选择态进签名；
  轮询只在选中变化时切 class，不重建列表。
- **token 输入框从未绑定事件**：粘贴 token 无反应 → 现在 change/Enter 生效，
  401 时顶部出现"需要 token"横幅并可一键聚焦输入框。
- **`/ui` 静态壳不再鉴权**（HTML 不含数据；`/ui/api/*` 仍强制 token）——
  无 token/旧 token 打开不再是 401 JSON，能看到粘贴入口。
- 连接探测加 4s 超时，不再长期停在"连接中…"；"未验证"从标题 ⚠ 改为 meta 小标签。
- 测试：+2（面板选择键/委托契约、账本 ID 唯一性）；**767 passed / 0 failed**。

## [6.0] — 2026-10-28（最终版：范围封板 · skill v1.9）

在 v5.0 基础上补齐 P1 剩余可验证项，并对不可验证/越界项**正式封板**（不是待办）：

- **读结果字段裁剪**：`read_blueprint` / `read_nodes` / `read_connections` 支持
  `fields=[...]`（MCP 与命令双通道），大图只取 title/guid/pos 等，省上下文。
- **DataTable 三命令**：`datatable_read`（行名 + CSV，5.1 无导出则降级）、
  `datatable_write_csv`（覆盖写 + 回读，需 confirm）、`datatable_create`
  （指定行结构，如 `/Script/GameplayTags.GameplayTagTableRow`）。
- **类能力探针** `class_probe`：只读检查 unreal 类/工厂/方法是否存在（排查用；
  本次用它确认 5.8 的 AnimBP 类表面与 5.1 的 DataTable 接口差异）。
- **修复真机发现**：5.1 的 CSV 接口在 `DataTableFunctionLibrary`（不是对象方法）→
  版本自适应；`get_row_names()` 返回 FName 导致 FastAPI 500 → 统一转字符串。
- **范围封板**（见 `docs/RELEASE-v6.0-最终版.md`）：AnimBP 状态机写入（无 Skeleton
  可验证）、namespace 分派（已有等价能力）、打包钩子（UAT 领域）等明确不做。
- 白名单 91 → **94**；工具仍 32；测试新增字段裁剪 5 项 + DataTable 4 项。
- 真机：5.1/5.8 DataTable 全链路、5.1 字段裁剪（MCP + 命令）、5.8 类探针。

## [5.0] — 2026-10-28（P1 资产治理：批量操作 + 任务级资产回滚 · skill v1.8）

P1 第一批（资产线），解决"新建撤不掉、删除找不回、批量搬迁靠手点"：

- **`batch_asset_ops`**：批量重命名/搬迁，默认 dry-run 只出预检计划；
  真跑需 `confirm=true`，逐条回读验证；目标冲突不覆盖；上限 50。
- **`task_assets`**：任务资产账本（created/deleted/snapshots），结束后可查。
- **`rollback_task_assets`**：删除本任务新建的资产（蓝图/材质/UMG 创建自动记账）
  + 从删除前快照恢复被删资产（`delete_asset` 前自动备份 .uasset）；
  只动账本内资产；需 `confirm=true`；恢复为文件级，需重载时返回
  `reload_required=true`。
- 白名单 87 → **90**；命令目录/文档/测试同步。
- 测试：新增 `test_asset_governance.py` 13 项；**756 passed / 0 failed**。
- 真机：5.1 + 5.8 各跑通"建→删→账本→回滚→批量重命名"全链路（快照 22KB/24KB 恢复）。
- 文档：`docs/RELEASE-v5.0-资产治理.md`。

## [4.9] — 2026-10-28（第一梯队 P0：上下文治理 / 会话保活 / 一键接入 / 行为验证 · skill v1.7）

按 `COMPETITIVE-v4.8` 的四轴结论落地的四项基础设施（工具目录保持全量，不做减法）：

- **P0-1 上下文治理（加法）**：新增 MCP 工具 `ue5_tool_search`（按关键词检索 87 条
  白名单命令 + 32 个工具，返回参数与一句话说明）；`ue5_execute_command` 描述去掉
  86 条命令名清单（2,347 → 533 字符）。工具描述总长 22.1k → 19.8k 字符（≈6.2k token），
  且**能力一个没少**（32 工具全部保留）。
- **P0-2 会话保活**：stdio 代理检测到桥离线后等待编辑器重启（默认 30s，
  `UE5_BRIDGE_RECONNECT_S` 可调），恢复后自动重发一次并重读 token；
  离线时报可读错误。模拟"编辑器重启"的回归测试通过。
- **P0-3 一键接入**：`install_mcp.py` 新增 ZCode 目标、客户端**检测**
  （只写已安装的，`--all` 强制）、`--dry-run`（默认不落盘）、幂等与备份；
  Python 3.9 兼容。
- **P0-4 行为验证**：新增 `verify_behavior` 命令（87 条）——读运行时属性与期望
  比对（exact/数值容差/contains），可选截图证据；断言失败 fail-loud，
  截图失败不翻转 verified；PIE 未运行给出 `pie_start` 指引。SKILL 新增 §3.10 SOP。
- 测试：新增 `test_verify_behavior.py` 9 项、`test_install_mcp.py` 5 项、
  stdio 重连 1 项；**全量 743 passed / 0 failed**（基线 727，+16）。
- 真机：5.8 验证 `verify_behavior` 通过/失败/PIE 未运行三条路径 + 截图证据；
  5.1 同路径（simulate）复测。

## [4.8] — 2026-10-28（工具目录恢复全量 + 竞争力对比 · skill v1.6）

**纠错**：v4.3 按「4 天调用日志 31 个只用 12 个」把默认工具目录收窄到 16 ——
这是统计错误（样本小、场景偏，未使用 ≠ 无价值）。本版恢复默认全量：

- `tools/list` **默认返回 31 个工具**；`UE5_MCP_CORE_TOOLS=1` 才是可选收窄档（16）。
- `UE5_MCP_ALL_TOOLS` 废弃（默认即全量，保留读取仅为兼容旧配置）。
- 相关测试与 SKILL 文档同步（默认 31 / opt-in 16）。
- 新增 `docs/COMPETITIVE-v4.8-功能维度效率美观.md`：对 UEBox / EngineWire /
  VibeUE / monolith / Claireon / 官方 5.8 做四轴对比（功能/维度/效率/美观），
  含本机实测的上下文成本（31 工具 ≈ 22.1k 字符 ≈ 6.9k token）与优化建议（P0–P2）。
- **面板移除「接入 AI」页**：面板正常是 agent 调起工具后才出现，此时连接早已建立，
  再展示配置属于顺序错误。接入前置到安装脚本/文档；`/ui/api/mcp-config` 端点保留
  （供安装器与外部工具使用）。导航从 3 项回到 2 项。

## [4.7] — 2026-10-28（管理端视觉刷新 · skill v1.5）

纯视觉/信息密度优化，无功能语义变更（测试基线保持 **726 passed**）。

- **参考来源**：本地 UEBox 源码的 `theme.css` / `typography.css`（双层 token、
  14px 正文、8/12px 圆角阶梯、150–250ms 动效）+ Linear/Vercel 式浅色仪表盘
  （纯白卡面、1px 级阴影、彩色只用于状态与数字）。
- **具体改动**：去掉卡片彩色左边框与 KPI 渐变；圆角 16→12/10/8 统一；
  阴影减淡；行高 1.7→1.65；三级文字加深一档；KPI/阶段卡悬停抬升统一 150ms；
  任务选中态加柔和内环；命令流水 summary 悬停反馈；`scrollbar-gutter` 防跳动。
- **信息密度**：运行状况页「技术层错误记录」折叠（与任务失败重复）；
  空状态从一句说明改为「下一步怎么做」。
- **可达性**：导航/任务项/筛选/折叠区补 `:focus-visible` 焦点环。
- **顺手修 bug**：`/ui/api/mcp-config` 在 HTTP 线程直调 `unreal.Paths`（违反
  GT 纪律）→ stdio 配置的脚本路径静默降级为 `<项目>/...` 占位符；改为 GT 通道
  + `normpath`，端点现在返回可直接使用的真实路径（供安装脚本/文档使用）。

## [4.6] — 2026-10-27（C++ 结构化编译诊断 · skill v1.4）

按既定顺序落地 **F 结构化编译诊断**（C → F → SKILL 分层，本版为 F）：

- **一次编译直读 `FCompilerResultsLog`**：bridge 新增
  `compile_blueprint_structured`，从 token（`FEdGraphToken` / `FUObjectToken`）
  直接取 `UEdGraphNode` → `node_guid` / 图名 / 节点标题 / 包路径（权威，不猜）。
- **消灭日志路径的三个先天缺陷**：不再依赖日志刷盘（不用 sleep 重读）；
  不靠标题反查 GUID（同名/本地化安全）；结果只属于本次编译
  （`may_be_stale` 恒 false，errors 不再混入上一轮）。
- **自动回退**：旧 DLL / 结构化异常 / `ok:false` / JSON 损坏 → 原日志正则路径，
  行为与 v4.5 完全一致；`structured:false` 是回退标志，不是错误。
- **返回体增强**：`structured` / `num_errors` / `num_warnings` /
  `errors_scope=compiler_results_log`；每条 error 带 `source` 与 `nodes[]`。
- `apply_graph` 编译失败分支同样优先结构化错误（修复循环定位更准）。
- 测试：新增 `tests/test_compile_structured.py` 12 项（优先/回退/字段/去重）；
  **726 passed / 0 failed**。5.1 + 5.8 真机各 8 项断言通过。
- 文档：`docs/RELEASE-v4.6-结构化编译诊断.md`。
- **SKILL.md 分层（渐进披露）**：1266 → 444 行，详细走查迁至 `docs/reference/`（modes / graph-apply / compile-fix / visual-check / task-reporting / cpp-api / version-history）；速查与硬规则留在主文件。
- 打包：`make_portable.py` 纳入 `docs/reference/` 与 v4.5/v4.6 发布说明。

## [4.5] — 2026-10-27（任务级撤销 · skill v1.3）

按既定顺序落地 **C 任务级撤销**（C → F → 分层，本版为 C）：

- **真实 UE 事务栈**：写命令自动各开一个事务，标题统一 `[agent] <cmd> <资产>`；
  一个命令 = 一个撤销步。嵌套调用（`editor_batch` 等）由 C++ 深度计数合并，
  不会碎成一堆步。
- **安全回滚**：`agent_undo(count)` 逐步校验标题前缀，遇到用户/引擎自己的事务
  **立刻停**并报告原因（不跳过、不误撤）。用户事务在场时写命令照常执行但不包事务
  （绝不插队关掉用户事务）。
- **新命令**（84 → 86）：`agent_undo_status`（可回滚步数/下一步标题/是否 agent 步）、
  `agent_undo`（1–500 步；0 步撤销 fail-loud）。
- **面板只读展示**：任务头部 `⟲ 可回滚 N 步`、复盘 `⟲ 本任务已回滚 N 步`；
  `/ui/api/task` 附 `undo` 字段（2s 缓存 + 2s GT 超时，编辑器忙时面板不卡）。
  不新增写路由，保持"管理端永久只读"铁律。
- **apply_graph**：写入段单独开事务、编译前提交；失败路径手工复原后 `Cancel`
  （不留空撤销步）。
- **UE 5.8 自启修复**：5.8 起不再执行项目自身 `Content/Python/init_unreal.py`，
  桥启动改挂 `ue5-automation-extras` 插件 init（幂等）。
- **构建脚本修复**：`build_for_engine.bat` 的 `)` 块解析 / `%APPDATA%` 转义 /
  CRLF / UAT 暂存路径两处兜底 —— 以前"编译成功但不部署"的假成功消除。
- 测试：新增 `tests/test_task_undo.py` 28 项；白名单计数同步 86；
  **714 passed / 0 failed**；5.1 与 5.8 真机各 8 项通过。
- 陷阱沉淀：`pitfalls.md` B18（`GetUndoCount` 实为可重做数 / `GetUndoContext`
  返回空 / 队列末位才是下一步）、B19（5.8 插件 init 是唯一自启入口）。

## [4.4] — 2026-10-27（实用底线三项 + 接入闭环 · skill v1.2）

按"能力完整、可竞争、简洁"三条标准，落地审计后 ROI 最高的四项（真机 8/8 通过）：

- **A 保存闭环**（数据丢失级）：`task_end` 前自动对本任务触碰过的资产做
  `only_if_is_dirty` 保存，返回 `auto_save{saved/clean/failed/missing}`；
  面板交付物区显示「✅ 已自动保存 N 个资产」/「⚠ 保存失败」。
  `UE5_AUTO_SAVE_ON_TASK_END=0` 可关闭（只检查不保存）。
  → 修掉今天真实踩过的坑：BP_Door 重启后消失、工具一声不吭。
- **B 死循环保护**（对标 UEBox loopBreaker）：同一「工具+参数」连续失败 2 次即拦截
  （编译类放宽 5 次），任何成功清零；带 300s 时间窗防长期累积误拦。
- **D 高危操作确认**：`delete_asset` / `delete_node` / `unpin_console_refs` /
  `apply_graph{replace:true}` 必须显式 `confirm=true`（顶层或 params 内均可），
  否则拒绝并说明。不破坏面板只读边界（参数级二次确认，非 UI 审批）。
- **S5 客户端接入闭环**：新增只读端点 `/ui/api/mcp-config` + 面板「接入 AI」页，
  一键生成/复制 HTTP(Streamable) 与 stdio 两套 MCP 配置（含 token）；
  stdio 的 command 自动纠正为引擎自带 python.exe（编辑器内 sys.executable 是
  UnrealEditor.exe，直接粘出去必失败）。
- 测试：+14 项守卫测试（高危确认/死循环/时间窗/成功清零/autosave 字段），
  conftest 加守卫状态隔离（防跨用例污染）；**686 passed / 0 failed**。

## [4.3] — 2026-10-27（全域调研后瘦身 · skill v1.1）

**动机**：用户要求"全网调研（不只 UEBox）+ 差异点筛选 + 砍掉不实用部分"。

- **MCP 工具分级 31 → 13（省 39% 常驻 token）**：依据我们自己的 4 天调用日志
  （31 个工具只用 12 个，未用的 19 个占常驻 5,932 token 的 44%）+ 跨领域数据
  （工具 >23 显著降低选择准确率；3 个 MCP server 吃 72% 上下文）。
  扩展 18 个工具仍可经 `ue5_execute_command` 调用（84 条白名单全量），
  能力零损失；`UE5_MCP_ALL_TOOLS=1` 可恢复完整目录。
- **管理端去重**：删除档案头部证据 chips（与阶段轨道"3 类证据"重复）。
- **SKILL.md 新增工具分级表**：列出扩展能力的 `execute_command` 调用方式。
- **新增 docs/ROADMAP-v4.3-全域调研与瘦身.md**：36+ UE 项目 + Blender/Unity/
  MCP 生态调研 → 9 项采纳 / 12 项不采纳（含理由）→ 自身瘦身记录。
  明确不打的仗：工具数量、桌面壳、美术能力、跨平台、性能剖析。
- 基线：672 passed / 0 failed

## [4.0] — 2026-10-27（定稿版 · skill v1.0）

**定稿前全量审计（28 项真机探测 + 5 层静态/测试检查）发现并修复 3 个问题：**

- **命令流水 hint 归位丢失（潜伏 bug）**：命令本应落到 plan 里 hint 匹配的步骤，
  但该逻辑在一次后续编辑中丢失 → 全部落到任务级/运行中步骤。已恢复
  （hint 双向包含匹配 → running 兜底 → 自动起步兜底），新增回归测试。
- **1s 轮询重置 UI 状态**：每轮重建 DOM 会把用户展开的「命令流水」每秒收起。
  改为**数据签名比较**——内容未变只刷新计时，不重建 DOM（任务列表同样处理）。
- **元命令污染流水**：`task_begin/step/end` 等记账命令不再进入命令流水。

**审计结论（可放行）**：
- 鉴权：`/ui*` 无/错 token → 401；`/command`、`/mcp` → 403（设计如此，`/health` 同属业务端点）
- 只读边界：`/ui` 的 POST/PUT/DELETE 全部 405
- 路径逃逸：`../../`、绝对路径、URL 编码变体 → 全部 404
- 参数 fail-loud：缺 goal / 非法 step_id / 非法 status / 空 nodes / 非法 source 全部拒绝
- 数据边界：超长 user_request 截断 1500、重复 begin 拒绝、limit 非法/超大容错
- 性能：任务 API 均值 **15ms/次**，`/ui` 页面 58KB
- 安全：工作树敏感信息 0 命中（仅存测试哨兵值，运行时用随机 token）
- 文档：manifest / SKILL / CHANGELOG / README 版本与计数一致（84 命令 / 31 工具）

**定稿基线：672 passed / 0 failed / 95 skipped + 27 subtests**

## [3.12] — 2026-10-27（人话表达 + 运行状况修正 · skill v0.27）

- **任务列表改用人话**：标题优先 `user_request`（用户原话，40 字截断、悬停看全文），
  agent 的术语 `goal` 仅作兜底；SOP 要求 `goal` 本身也必须写成用户口语。
- **运行状况页修正（用户发现的 bug）**：旧实现只统计 **HTTP 事件**（进程内存、
  重启清零），完全不看任务结果 → "任务列表有失败但运行状况全部成功"。
  现在新增**任务结果**区块（持久化数据：总任务/成功/失败/进行中）+ 失败任务清单
  （含原因摘要），并把调用统计明确标注为「本次进程（重启清零）」。
- **表达分层纪律写入 SOP**：工具负责事实（命令/路径/编译结果，自动记账），
  agent 负责**人话**（改了什么 · 为什么 · 怎么确认）。`deliverables[].note` 必须
  写三要素；`how_to_verify` 必须是用户视角步骤（不含命令名/参数）。
- **跨日回放**：只读当天会让"昨晚的任务今天打开就没了" → 回放最近 3 天会话文件。
- 文案简化：阶段轨道与卡片副标题改为口语（"你说的 / 怎么做 / 做到哪了 / 改了什么"）。
- 真机验证：BP_Door「碰到门有提示」——任务列表显示原话、交付物三要素、4 步验证；
  运行状况显示 14 任务 / 11 成功 / 3 失败 + 失败清单。
- 基线：671 passed / 0 failed

## [3.11] — 2026-10-26（交付物清单 + 排版审计 · skill v0.26）

- **交付物清单（用户要求）**：`task_end` 新增 `deliverables`（[{kind,path,action,note}]）
  与 `how_to_verify`（字符串或步骤数组）；面板第 4 段「交付物与验证」以表格
  呈现「在项目里改了什么 / 在哪 / 动作类型」，并生成分步验证指引。
  agent 未填时从命令流水**自动汇总**（资产去重、截图单列、编译结果作验证），
  并显式标注「自动汇总」。
- **排版审计与修正（用户反馈证实）**：
  - 字号从 **17 档收敛为 6 档**（11/12/13/14/15.5 + 大标题/KPI），
    消除 10.5/11.5/12.5 等 0.5px 相邻档造成的「不协调」
  - 任务列表补齐**明确边界**：白底 + 1px 边框 + 左侧 3px 选中色条 + 悬停抬升；
    标题 13px/两行截断、元信息 11px 去 mono（中文不再参差）
- **修复 JS 整块失效**（真机）：`how_to_verify` 渲染里的跨行正则导致整个
  `<script>` 解析失败（TOKEN/pollTask 全 undefined、面板空白）→ 改写为
  `String.fromCharCode(10)`；新增回归测试（脚本段跨行正则扫描）
- `how_to_verify` 支持数组 → 按行拼接（面板渲染为有序列表，不再出现方括号）
- 真机验证：BP_PortalDemo「进入提示 + EnterCount 变量」，交付物 2 项、
  5 步验证清单、编译通过
- 基线：672 passed / 0 failed

## [3.10] — 2026-10-26（用户视角全链路 v4 · skill v0.25）

- **修复「任务成功但步骤进度不动」**（真机 bug）：v3.9.2 之前生成的历史任务，
  步骤停在 running/pending → 新增惰性封口 `_normalize_finished`（读取时幂等迁移）
  + 回放迁移 + 面板对已结束任务显示「已收尾」；进度条与 x/y 步现在一致。
- **功能区重构（阶段轨道 + 独立卡片）**：顶部 4 段轨道（①需求来源 → ②实现逻辑
  → ③执行脉络 → ④复盘，当前段高亮/完成段打勾，点击跳转）；每段独立卡片
  （左侧色条 + 序号徽章），解决"各模块挤在一起、看不清流程"。
- **全链路可视化（用户诉求）**：
  - `task_begin` 新增 **`user_request`（用户原话）** → 面板「你的原话 vs 我理解成」
    左右对照（原话逐字引用 + 对齐要点清单）
  - **命令流水**：每个步骤下显示下发了什么命令、参数摘要、UE 返回
    （verified/compiled/节点数/错误）、**UE 内实际动作**（内置 15 命令解释表）；
    超 3 条折叠
  - 命令按计划步骤 `hint` 自动归位（不手动推进也能一一对应）
  - 失败原因 `error` 纳入命令流水（消除「verified=false 但 errors=[]」判读黑洞）
- **`task_begin` 自动打开管理端**（进程内一次；`UE5_AUTO_OPEN_PANEL=0` 关闭），
  轮询降到 1s（等效实时）。
- 真机验证：BP_PortalDemo「重叠触发→打印」全链路——apply verified=true /
  compiled=true / 5 步 100% 已收尾 / 三类证据齐全。
- 基线：667 passed / 0 failed。

## [3.9] — 2026-10-25（双引擎真机验证与发布收敛 · skill v0.24 · 发布版）

- **双引擎真机全链验证并出报告**（`docs/VERIFICATION-RELEASE-3.9.md`）：
  UE 5.1.1 + 5.8.2 两个本地真实工程逐项实测
  建图/写图/编译诊断/修复收敛/截图三源/投影/管理端，主路径全部通过。
- **5.8 UFUNCTION 非 GT 拒载修复（B13）**：C++ `ExecutePythonOnGameThread`
  跳板在 5.8 被派发层拒绝 → 新增 `_gt_exec_python` 走本模块 GT 队列直呼
  （TEST_MODE 保留旧跳板以维持 D8 mock 契约）；`/build` 首次在 5.8 端到端跑通。
- **投影矩阵轴重映射（B7 同族最终解）**：`FInverseRotationMatrix` 局部视空间
  （X前/Y右/Z上）与 `FReversedZPerspectiveMatrix` 的 Z=深度约定不符；按 UE
  行向量约定（P·M）插入轴重映射，官方 MCP `WorldPosToScreenCoords` 真值对照
  NDC 误差 <1e-3；视锥外坐标置零（防栈残留误当有效位置）。
- **整图写入读回修复（B14）**：新建节点 `node_title` 在 5.8 DLL 裸包装不可达
  （恒 ''，NodeGuid 同族）→ 连线确认标题取不到时跳过拓扑比对（引脚级已证），
  读回验证改用读回节点本地化标题（GUID 映射），映射不到降级 + 显式告警。
- **编译诊断中文日志双变体（B15）**：5.8 `LogBlueprint: Error: [AssetLog]路径：[编译器]…`（全角冒号）
  与 5.1 `[AssetLog] 路径 (no disk path found): [Compiler] …`（半角冒号+附加段）
  统一正则支持；编译失败且零提取时等 2s 重读（日志异步刷盘）。
- **守卫定案（B16）**：`bBeingCompiled` 为 transient 位域、5.1/5.8 Python 包装器
  均不暴露（`debug_reflect` 探针实测 dir() 无任何 compil 成员）→ 纯 Python 守卫
  不可实现；实际防线 = GT 队列串行化。告警文案改为如实说明。
- **新命令 `debug_reflect`**（白名单 83→84）：资产反射探针（候选名 × 两种读法 +
  dir 子串扫描），沉淀 UPROPERTY 读法排查方法。
- **事件流 `/health` 过滤**：管理端 5s 连接探测不再刷屏事件流/虚增调用统计
  （新增 `test_health_probe_not_recorded` 锁定）。
- **信任与体验（T-20261025-UX）**：
  - **success 需证据**：`task_end(success)` 若全程无编译诊断/整图写入/截图，
    打 `unverified` + warnings，管理端显示「⚠ 未验证」徽章（防 agent 自述成功）
  - **自动进度**：关键工具调用自动落入进行中步骤的过程日志（含耗时），
    步骤从未上报时自动起步第一步；收尾自动封口悬挂步骤（`auto_closed`）
    —— agent 最少只需 `task_begin` + 干活 + `task_end`
  - **判读字段**：`compile_and_diagnose` 返回 `errors_scope` /
    `convergence_signal` / 每条 error 的 `may_be_stale`（消除「errors 非空
    = 没修好」的误读）
  - **管理端**：任务列表状态筛选（全部/进行中/失败/成功）+ 列表内证据角标
    （编 n/写 n/图 n）+ 档案头部证据 chips
  - **文档**：新增 `docs/QUICKSTART.md`（人类用户 10 分钟上手）；
    SKILL.md 顶部「30 秒路由」决策树
  - **英文引擎**日志样本单测（结构同中文，正则已覆盖）；
    测试子集运行不再依赖导入顺序（token 现读化）
- **版本号统一**：manifest（skill v0.24 / package v3.9）、SKILL、README.zh-CN、
  START-HERE（84 条命令 / 31 个 MCP 工具）。
- 离线基线：**659 passed / 0 failed / 95 skipped + 27 subtests**。

## [3.8] — 2026-10-25（管理端 v3 任务指挥台 · skill v0.23 → v0.24）
- **管理端 v3「任务档案」重构（依据用户《可视管理需求.txt》）**：v2 按面板
  类型组织、用户"看不明白脉络"——v3 改为**一个任务一页故事**：KPI 条
  （总任务数/成功率/进行中/累计耗时）+ 任务选择器（默认最新，可翻历史）+
  档案四段（①需求确认 ②实现思路 ③执行脉络 ④复盘）。截图对比降级为
  **步骤内 evidence.pair 配对证据**（改前/改后并排 + 说明文字），不再有
  脱离语境的独立对比面板；"按工具调用统计"降级进调试抽屉。
- **任务日志 task_journal（新模块）**：`ue5_task_begin/step/end/list/error`
  五命令 + 四个 MCP 工具（白名单 78→83，工具 27→31）；agent 上报意图/
  计划/进度/复盘，管理端据此渲染。`understanding` 语义 = **需求确认书**
  （与用户对齐的要点，开工前上报，用户可当场纠偏）。当日任务板随
  bridge 重启自动回放；被中断的任务收尾为 aborted（绝不永久"进行中"）。
- **SKILL.md §3.9 任务上报 SOP**：需求前置（聊清需求→先上报再动手）、
  evidence 配对形态、复盘的 summary/lessons 写法（经验分析供下次规划）。
- **真机验收（本地 5.8 工程）**：用户故事任务（移动石柱到视口
  中心）全链路——需求回显 → 四步执行（投影坐标 (1208,589) 正中视口中心
  (1209,587)）→ 配对对比图 → 复盘卡；白底主题。
- 测试 656 passed / 0 failed / 95 skipped（+21：journal 单元 / MCP 链路 /
  端点鉴权 / 截图路径逃逸拒绝 / DOM 契约随 v3 更新）。

## [3.7] — 2026-10-24（截图反馈闭环 · skill v0.23）
- **截图反馈闭环（P0 规格书《P0-截图反馈闭环设计.md》· 给 agent 装上"眼睛"）**：
  修改 → 截图 → 视觉自检 → 修正 的闭环成型。blender-mcp（32,148★）的明星功能
  是"AI 检查自己的产出"；UE 赛道此前仅 UEBox 做了截图+actor 标注。本版按
  UEBox 源码透析核实的 5 条经验实现（机位溯源必回传 / PIE 延迟一帧纪律 /
  最小化窗口白图规避 / 图片直接进上下文不走 URL / 压缩阶梯对齐 768）。
  - **★ C++ 部分为离线草稿，未经编译验证 ★**：本机无引擎无 VS，桥侧实现
    写成"结构正确、模式一致、TODO(home) 清晰"的草稿状态 ——
    `bridge/BlueprintPythonBridge/Source/BlueprintPythonBridge/Private/BlueprintCapture.cpp`
    （新文件）+ `.h` 三个 UFUNCTION 声明（CaptureViewport / CapturePIE /
    ProjectActorsToScreen，恒返回 JSON 字符串）。每个文件头部有醒目 TODO(home)
    块：回家先 `build_for_engine.bat <引擎根>` 编译 5.1/5.8 排错（文件内标注了
    6 处 API 形态级风险点：GCurrentLevelEditingViewportClient 符号、
    FScreenshotRequest 绝对路径行为、FReversedZPerspectiveMatrix 重载语义等）。
    **旧 DLL 下 capture 命令会得到明确的"重新编译"提示（fail-loud 降级，绝不假成功）**。
    实现路线（UEBox 同款）：编辑器世界 SceneCapture2D→RT(RTF_RGBA8+显示伽马)→
    ReadPixels→PNG 落盘（同步）；PIE 走 FScreenshotRequest（延迟一帧 → pending，
    **在 GameThread 上轮询文件 = 阻塞帧推进 = 自死锁**，故 pending 收尾一律在
    非 GT 线程：宿主侧轮询存在+大小稳定，2s 超时 fail-loud）；最小化窗口先
    Restore（白图规避）；agent_camera 给确定性机位（同机位环绕连拍）；
    ProjectActorsToScreen 用手搓 ViewProjection 把世界坐标投到与截图同一机位
    的屏幕像素（视锥外 visible=false，找不到的 actor found=false，绝不编造）。
    输出路径强制约束在 `<项目>/Saved/Screenshots/ue5-automation/` 下（防逃逸，
    宿主预检 + C++ 前缀裁决双层；该目录同时是仪表盘画面面板的数据源 ——
    仪表盘侧零改动）。
  - **新白名单命令 3 条（75→78）**：`capture_viewport`（active_viewport |
    agent_camera 机位；PIE 中自动转游戏画面）· `capture_pie`（PIE 未运行
    fail-loud）· `project_actors_to_screen`（标注数据源）。白名单 + handler
    双侧同步；旧 DLL 无 API → 干净降级报错。
  - **新 MCP 工具 `ue5_screenshot`（26→27）**：参数 source
    （editor_viewport | agent_camera（camera 必填）| pie）/ camera / annotate
    （默认 true）/ max_dimension（默认 768，与 UEBox 压缩阶梯对齐，钳制
    [64,2048]）/ out_name。返回 MCP image content block（base64 JPEG q80，
    长边压缩）+ text 元数据（saved_path / camera_used+camera_source 溯源 /
    annotated_actors / warnings）—— **base64 不进文本块**，全尺寸原图只落盘
    不进上下文（控 token）。**Pillow 为可选依赖**（不进 requirements）：
    缺失 → 跳过标注 + PNG 直通（≤max_dimension 时）或明确告警不返回图
    （超长边防 base64 灌爆），**绝不因缺 Pillow 失败**；文件读取回验证
    （存在+非空+可解码+尺寸）不符 fail-loud。协议层 `_mcp_handle_one` 支持
    image content block 拆装 + `ue5_screenshot` 特判 HTTP 线程直呼
    （GT 死锁规避，见上）+ `_capture_finish_pending` 端点钩子（/command、
    ue5_execute_command、editor_batch 步骤三条路径的 pending 收尾）。
  - **fake_unreal 替身扩展**：capture 三命令写**真实 PNG**（纯 stdlib
    zlib+struct 手写编码，零依赖）；注入口 `set_capture_delay`（延迟一帧
    模拟）/ `set_capture_not_write`（超时回归）/ `set_capture_fail` /
    `set_pie_world` / `set_projection_result`（可注入投影表）/
    `set_capture_size` / `Paths.set_project_dir`；`reset_fake()` 全量复位。
  - **测试 `tests/test_screenshot.py`（41 用例）**：三命令契约（success 键/
    落约定目录/bytes 回填/camera_source 溯源）、路径逃逸双层拒绝（5 样本
    表驱动）、PIE 未运行 fail-loud、pending 两条路径（0.3s 轮询等到 /
    2s 超时 fail-loud）、GT 死锁规避（_GT_TASK_RUNNING 模拟 → 标记 +
    端点钩子收尾，含 editor_batch 步骤递归）、旧 DLL 降级（3 命令 + BRIDGE
    不可用）、MCP 工具（无 Pillow PNG 直通/超长边不返回图/有 Pillow 标注
    +压缩+JPEG、可见性过滤、agent_camera 缺 camera、损坏文件 fail-loud）、
    纯函数单测（PNG 尺寸解析/等待稳定判定）。全量 **635 通过/0 失败/95
    跳过 + 27 子测试**（基线 594+41 全保持；计数断言 6 处同步 78/27）。
  - **文档**：MCP-ACCESS 工具清单 26→27（新增视觉行 + 视觉闭环工作流）、
    README 工具计数、SKILL.md v0.23 + §3.8 视觉自检 SOP（截图→自检→修正，
    一句话规则：**改完必看一眼 —— 每轮视觉相关的修改后调 ue5_screenshot
    确认画面真的变了，再看不到预期的变化就别声称成功**）。

## [3.6] — 2026-10-24（管理端观测面 · SKILL.md 不变 —— 仪表盘不是 agent 用的功能）
- **管理端可视化仪表盘 v1（P1 规格书《管理端仪表盘设计》· /ui 只读观测面）**：
  给"可验证自动化"装上可见的仪表盘——任务流水、图纸视图、验证墙、画面、报告。
  **不是 UEBox 式驾驶舱**：只读、无壳、无模型接入、可有可无（整套工具无仪表盘
  时照常无头运行）。
  - **事件管道**：全局中间件 `add_trace_id`（所有请求唯一咽喉——stdio 代理
    固定转发 8889，无论哪个 MCP 客户端流量都过这里）追加事件记录
    `{ts, trace_id, route, method, status, latency_ms, tool_name, category}`
    → 内存环形缓冲（1000 条）+ 追加写 JSONL 会话文件
    （`%APPDATA%/ue5_bridge/sessions/YYYYMMDD.jsonl`）。
    tool_name 由 **/mcp 处理器**解析请求体后挂到 request.state（中间件收尾
    读取）——刻意不让中间件读 body：流式 body 只能读一次，读了就毁 MCP 客户端
    连接。分类映射表（preflight/读/写/编译/验证/保存）是显式字典：tool 级优先
    于 route 级，未命中 → other，**绝不猜**（分类错了比不分类更误导）。
    /ui* 自访问不入流（页面 500ms 轮询否则会把自己灌满——防自激）。
  - **★ 刻意的 fail-open（对 fail-loud 纪律的唯一明确例外，全仓仅此一处）★**：
    事件记录与快照缓存的任何异常（磁盘满 / JSONL 不可写 / 缓冲异常）一律吞掉，
    **绝不阻塞命令主路径**。理由：观测面失效不许影响业务——agent 写操作的可信度
    由返回体 verified/warnings 承担，不依赖仪表盘；一个"为了让人类看见"的旁路
    把命令弄挂是本末倒置。该例外只覆盖事件记录与快照缓存两处；/ui* 只读端点
    本身照常 fail-loud（查不到就 404/401，不假装成功）。中间件里另有外层
    try/except 双保险 + 测试注入必然抛错的记录器回归锁定。
  - **只读 API（全 GET，不新增 MCP 工具，tools/list 保持 26）**：
    `GET /ui`（单文件静态页）· `GET /ui/api/events?since=<id>`（增量轮询 +
    验证卡片增量）· `GET /ui/api/graph?asset=`（最近 reader L2 / GraphSpec
    快照，内存 LRU 缓存最近 8 个资产；未命中 404 + 清单，不假装成功）·
    `GET /ui/api/reports`（报告列表 + JSONL 会话清单）·
    `GET /ui/api/reports/export`（审计导出 zip：全部 JSONL + 报告文件 +
    manifest.json——"可验证"从 README 文字变成可下载的实物凭证）·
    `GET /ui/api/screenshot`（画面面板数据源，截图闭环未就绪 → 404 占位）。
    鉴权复用 `_verify_token`（X-Skill-Token / Bearer / ?token= 三通道），
    失败 401（区别于业务端点 403：浏览器人面"未认证"语义更准）。
    ★ /ui* 专用放宽：token 允许走查询参数——页面是浏览器直接打开的静态单页，
    无法配自定义 header；服务只绑 127.0.0.1 且全 GET，token 泄露面 = 本机
    浏览器历史，风险可控（与 _verify_token 既有 ?token= 兜底同一口径）。
    只读性由测试遍历 app.routes 锁定（/ui* 无 POST/PUT/DELETE/PATCH，且路由
    白名单外新增即报错）。
  - **图纸快照挂钩（单向依赖 bridge → ui_dashboard，反向会成环）**：
    `/read` 端点（blueprint_reader 宿主 CLI 数据源）与 /mcp 的
    read_nodes/read_connections/read_blueprint → reader L2 归一快照；
    ue5_apply_blueprint_graph → GraphSpec 快照 + node_guids + 结构化编译错误；
    ue5_compile_and_diagnose / ue5_compile_blueprint → 给已有快照更新编译错误钉。
    归一形态 `{nodes:[{id,title,kind,pos,guid}], edges:[{from,to}],
    meta:{verified, compile_errors, node_guids}}`——页面只认这一种。
  - **静态页 `ui/index.html`（单文件 HTML + 原生 JS/CSS，无框架无构建链）**：
    五面板单页——任务流水（六类图标 + 耗时 + 状态色 + 分类过滤）、图纸视图
    （SVG 矩形节点 + 带箭头正交折线；坐标优先 GraphSpec/reader 既有 pos，
    没有则前端 Kahn 分层摆放；最近写操作 node_guid 呼吸高亮；编译错误红标钉
    节点 hover 显示 message+fix_hint，钉不到的走图例兜底）、验证墙
    （verified/warnings 卡片流、失败置顶红色、§3.6 编译轮次 x/5 从事件流
    推断）、画面（等待截图闭环占位）、报告（列表 + 审计包导出）。顶栏 token
    输入框（localStorage 记住）+ 连接状态指示。轮询：事件 500ms / 图纸 1.5s /
    画面 5s；流水 DOM 上限 200 行防长会话膨胀（事件本体在服务端环形缓冲）。
  - **测试 `tests/test_dashboard.py`（25 用例）**：事件正常记录 / 注入抛错记录
    器不阻塞 / 环形缓冲上限 / JSONL 落盘格式 / 自访问过滤 / tool_name 挂钩 /
    分类映射 / 401 / 查询参数 / 增量游标 / LRU 缓存 / 归一化 / 错误钉 /
    验证卡片 / zip 导出 / 画面占位 / 只读性 / tools/list=26 / 页面 DOM 契约。
    测试模式下 JSONL 默认落临时目录（UE5_BRIDGE_SESSIONS_DIR 显式指定优先），
    不污染真实 %APPDATA%。

## [3.5] — 2026-10-24（skill v0.22）
- **整图声明式写入（P1 规格书 · UEBox 源码透析主路径；全程零 C++ 改动）**：
  UEBox 删除了全部逐节点写图工具，第一条规则是 "Never build a graph one node
  at a time"（逐节点改图是上下文与轮次黑洞）。本库**保留**逐节点手术式编辑
  （fail-loud 纪律的用武之地），另增「查一批 → 写一整张」主路径。
  - **新白名单命令 `search_blueprint_nodes`（只读）**：按名字搜可调用函数/事件/
    流程控制/变量候选，**连完整引脚签名一起回**（签名与 `_read_l2_nodes` 同源
    get_pin_infos，类型名走既有归一化口径）。搜索源按可信度排序：图内已有节点
    → 蓝图自有函数图（入口/出口引脚聚合）→ 变量 get/set（类型合成签名）→
    流程控制目录（Branch/Sequence/ForEachLoop/WhileLoop/DoOnce/Gate/FlipFlop/
    MultiGate/IsValid）→ 标准事件目录（ReceiveBeginPlay… + BeginPlay 友好名
    确定性映射）→ 父类/函数库候选（dir() 枚举；**签名如实缺省 + pins_note**
    —— 纯 Python 通道读不到 UFunction 反射签名，UEBox 用 C++ GetAllFunctions，
    本版零 C++，绝不假装给全）。query 子串大小写不敏感；limit 上限 50 防响应
    洪水（超限收敛 + warning）。
  - **新白名单命令 `apply_graph`（声明式整图写入）**：提交 GraphSpec
    （nodes/edges/pin_defaults，模型生成带 caller-id），一次事务内建节点 +
    连线 + 设默认值。执行序：**双守卫**（蓝图 `bBeingCompiled` → 409 Conflict
    语义拒绝，编译期间图结构不稳定；图非空且未显式 `replace=true` → 拒绝并
    返回当前图摘要 —— 替换语义 + 让模型先 read 再决定，绝不静默合并）→
    replace 清图（函数图的 FunctionEntry/FunctionResult 结构节点保留，否则
    函数图就废了；引擎拒绝删除（被外部引用）→ 逐条列出并失败）→ **事务内**
    建节点（复用 add_event_node/add_function_call_node/add_macro_node/
    add_node_by_class/add_variable_node 的引擎调用层；函数名 K2_ 候选重试与
    add_function_node 同款）+ 连线（复用 connect_pins 的拓扑验证语义：建链 +
    双端引脚级回读 + 标题唯一时拓扑回读）+ pin_defaults（设值 + 回读）→
    提交 → **事务外编译**（UEBox 纪律，与 compile_blueprint 共用
    `_compile_bp_and_scan_log`）→ **读回验证**（节点按 GUID 锚点在位性 +
    连线在位性；引擎悄悄多插的**隐式转换节点记 warning 不算失败**（R1 降级
    口径，与材质族 v0.20 verified 语义对齐）；读回通道不可用 → verified=false
    + 告警，「验证没发生」是一等信号，不制造假阴性）→ **失败即回滚**
    （显式追踪 + 逆序 remove_node + 复读确认图与事务前一致，对齐 /build-batch
    的「失败回滚已建资产」模式；回滚未复原 → 显式「图已污染」报错）。
    **自动布局**：确定性拓扑序网格（Kahn 分层，列 ×300 行 ×240，有环剩余按
    声明序追加尾层；显式 pos 优先）—— 纯 Python，**不引 elkjs**（EPL-2.0
    许可边界）；相同 spec → 相同坐标。
  - **GraphSpec 解析容错（宽容形态、严格语义）**：graph 缺省 EventGraph、
    edges/pin_defaults 可缺省（只落节点合法）、kind 别名 get/set 归一、节点
    可选 pos 跳过布局；非法即 fail-loud：nodes 非空数组、id 唯一且不含点号
    （edges 用 "id.pin" 点语法）、kind 白名单、edge/pin_defaults 引用必须已
    声明、未知 flow target / 不存在的变量绝不猜。解析失败/守卫拒绝/回滚/
    读回不一致/编译失败 → 返回体带 `_fail_loud`（/command 端点层 success
    同步置 false —— 写图主路径的失败必须在端点层可见）。
  - **返回体**：`{success, compiled, compile_status, verified, nodes_applied,
    edges_applied, node_guids{spec_id: GUID}, errors[], warnings[],
    rollback?, graph_summary?}` —— node_guids 是修单点（set_pin_default_value
    /delete_node）的定位键；编译失败时图保留、带结构化编译错误
    （`_extract_blueprint_errors` 复用），直接进 §3.6 修复循环（编译失败
    ≠ 推倒重来）。
  - **MCP 工具 `ue5_search_blueprint_nodes` + `ue5_apply_blueprint_graph`
    （24→26 个）**：v3.3 同款双语描述 + 逐参数 schema；描述**互相交叉引用**
    并引用逐节点工具（UEBox 教训：改描述不同步 → 模型照旧文调错工具）：
    Use-when = search「写 apply_graph 之前，先拿到准确的引脚签名」/
    apply「新建或大改一段图逻辑；逐节点工具仅用于手术式微调」。
- **SKILL.md §3.7 整图声明式写入（写图 SOP + 硬规则）**："Never build a graph
  one node at a time" 原则（>2 节点的新图一律 apply_graph）；写图 SOP =
  search_nodes 拿签名 → 构造 spec → apply_graph → 看 verified → 编译失败走
  §3.6；硬规则：先 search 后写 spec（不编造引脚名）、非空图不静默覆盖、
  编译失败 ≠ 重写。版本链 v0.21 → v0.22 + 历史下推；MCP-ACCESS /
  START-HERE / README.zh-CN 工具数与命令数同步（24→26 / 73→75）。
- **`fake_unreal.py` 扩展**：`add_function_call_node` / `add_event_node` 替身
  （此前这两条 API 只被 /build GT 模板与 _cmd_add_function_node 走到，离线
  零覆盖；语义对齐真实 DLL —— FindFunction 不命中 → None，函数签名目录 +
  蓝图自有函数经资产库同名蓝图解析）；`being_compiled` 标志 +
  `get_editor_property("bBeingCompiled")` 反射读法（409 守卫注入口，
  `set_being_compiled()`）；`set_graph_apply_insert_converter()` 隐式转换节点
  注入口（连线成功后往图塞 spec 外节点，驱动读回验证 warning 降级路径）；
  `KismetSystemLibrary` / `GameplayStatics` / `Actor` 引擎 Python 包装面
  （search 的 dir() 枚举通道）；`reset_fake()` 复位。
- 新增 `tests/test_graph_apply.py`：**38 个用例（+5 子测试）**，覆盖规格书
  测试计划全部 8 组 —— search 命中/截断/大小写/签名完整性（含引擎候选如实
  缺省断言）/只读纪律；apply 正路径（3 节点 2 边 + verified + GUID 锚点 +
  连线/默认值落图断言 + 事件别名 + 自定义事件 + 变量 get/set + 自有函数 +
  K2 候选告警）；replace 守卫（拒绝+摘要 / 清空 / 函数图结构节点保留 /
  结构节点不计空判定）；原子性（5 类坏 spec 零改动 / 连线失败回滚 / 建节点
  失败回滚 / 设默认值失败回滚 / 失败后可立即重试）；编译中守卫（409 语义 +
  零改动 + replace 不可绕过）；读回验证（隐式转换 warning 不算失败 / 同名
  降级告警）；MCP 契约（success 键 / isError 双向 / 白名单 75 / 工具 26 /
  描述交叉引用断言）；布局确定性（同 spec 同坐标 / 拓扑网格 / 显式 pos 优先 /
  无 elkjs）。test_pywrap_l1/l2/v3_fusion/test_compile_diagnose 的计数回归
  同步 73→75 / 24→26（历史沿袭，追加批次注释）。
- 离线回归：**568 通过 / 0 失败**（95 跳过 = 需活体编辑器的在线用例；
  基线 530，新增 38）。

## [3.4] — 2026-10-24（skill v0.21）
- **编译报错自动修复闭环 · 诊断侧（P0 规格书第一块 + 第二块；全程零 C++ 改动）**：
  设计原则 = **工具提供「结构化的错误 + 可定位的锚点」，修复决策交给模型** ——
  不搞启发式自动修（错误修复的空间太大，启发式会制造假成功，与 fail-loud
  纪律冲突）。修复循环协议写在 SKILL.md §3.6，工具只负责把每轮反馈做「可定位」。
  - **新白名单命令 `compile_and_diagnose`（72→73 条）**：编译 + 结构化错误抽取。
    把原 `compile_blueprint` 的「编译 + 日志尾部 200 行 error_patterns 扫描」抽成
    公共函数 `_compile_bp_and_scan_log`（两命令共用同一份实现，避免拷贝漂移；
    `compile_blueprint` 返回体键集合与语义**完全不变**，零回归面）。
  - **结构化错误抽取**：解析 `LogBlueprintUserMessages: [资产 函数 [节点标记] 节点名] 正文`
    报错行，容错中文引擎（`BP_Pickup OnOverlap 节点 Branch`）与英文引擎
    （`/Game/Dir/BP_Pickup.BP_Pickup OnOverlap Node Branch`）两种格式，函数名可缺、
    标记可缺。**解析不出就降级 null，绝不编造**：无标记的多 token 上下文不猜节点名
    （信息保留在 message 原文）；短名资产不拼造 `/Game/` 路径（真实资产可能在任何
    目录，编造路径会把 agent 引向不存在的资产）；短名也不剥 `_C` 后缀（资产名本身
    就可能叫 `X_C`，为不出现的格式引入系统性误判不划算）。
  - **节点名 → GUID 映射**（闭环铰链）：复用既有读取接口 `_read_l2_nodes`
    （其 guid 字段已做过 UE5.1 `<Struct 'Guid'>` 归一化修复，与 `get_node_by_guid`
    直通）。**同名标题歧义整条剔除**（PTN-018 教训：同名节点仅按 title 定位不可靠）
    → `node_guid: null` + warning，绝不随机取其一。
  - **规则映射**：每条错误按整行匹配 `error_patterns.json`（与 log_matches 同口径）
    → `pitfall_ref`（pattern id）+ `fix_hint`（pattern 的 fix_action）+ 归一化
    `code`（pattern 的 error_code；未命中 → 哨兵值 `CompileError.Unclassified`）。
    **fix_hint 仅供参考，不代替模型判断**（返回体固定携带 `fix_hint_note` 声明）。
  - **返回体**：`{success, compiled, compile_status, errors[], warnings[], fix_hint_note,
    log_matches?, error?}` —— `success` 语义与 `compile_blueprint` 一致（编译成功为
    True；MCP 层 isError 判据依赖该键存在），错误列表即使编译失败也尽力给全；
    日志读不到时 `errors=[]` + `warnings` 明示原因（「errors 为空 ≠ 没有错误」，
    「没读到日志」与「日志里没有报错行」可区分）。
  - **MCP 工具 `ue5_compile_and_diagnose`（23→24 个）**：v3.3 同款目录条目
    （双语描述 + `bp_path` 参数 schema），Use-when = 编译失败需定位修复时替代
    `ue5_compile_blueprint`。
- **SKILL.md §3.6 编译修复 SOP（规格书第二块，只写协议不写新代码）**：
  修改蓝图后必须编译、失败进入循环；每条错误按 `node_guid` 定位 → 增量编辑工具修
  → 重编译。硬规则：**最多连续 5 轮**（第 5 轮仍失败 → 停止报告已尝试方向）；
  **同参数重复失败 2 次提前熔断**（UEBox `loopBreaker` 经验，成功的写操作清零计数
  —— "改→编→再改→再编"是正常循环）；**每轮事务回滚点**；**禁止循环中保存资产**；
  **fix_hint 冲突时以模型对连线/语义的分析为准**；**修后必须 read-back**（编译通过
  ≠ 图是对的）；**"Unknown ≠ 失败"措辞纪律**（Live Coding 类无结果 API 必须如实说
  "不知道成没成"，绝不报成失败 —— 否则会引导模型去改本来没问题的代码）。
  版本链 v0.20 → v0.21 + 历史下推；MCP-ACCESS / START-HERE / README.zh-CN 的
  工具数与命令数同步（23→24 / 72→73）。
- **`fake_unreal.py` 扩展**（对齐材质族故障注入风格）：`Paths.set_log_dir()`
  日志目录注入（离线测试用 tmp 目录写真实日志文件）+ `set_compile_result()`
  编译结果开关（bridge 与 BlueprintEditorLibrary 两条编译路径共用，驱动
  「编译失败 → isError=True」契约测试）；`reset_fake()` 复位。
- 新增 `tests/test_compile_diagnose.py`：**26 个用例（+22 条表驱动子测试）**，
  覆盖解析器单测（22 条真实风格样本：中/英文引擎、带路径/短名、`_C` 类后缀、
  函数/节点缺省、状态机子图、无标记降级、多错误混合、重复行去重、引脚提示）/
  降级路径（日志不可读、GUID 映射不到、同名歧义、Bridge 缺失）/ 规则映射
  （命中与未命中）/ MCP isError 契约 / 白名单与工具目录计数回归。
- 离线回归：**530 通过 / 0 失败**（95 跳过 = 需活体编辑器的在线用例；基线 504，
  新增 26）。

## [3.3.1] — 2026-09-23（skill v0.20）
- **材质族写操作补回读验证（把蓝图侧的 P04 教训平移过来）**：
  蓝图侧早已证实「引擎 API 的 bool 返回值会说谎」（`TryCreateConnection` 是
  *可行性验证* API，类型不匹配时建隐式 Cast 节点仍返回 True → 现走四重验证）。
  材质侧 `connect_material_expressions` / `connect_material_property` 是**同类
  API**，原先只判返回值 → 同一类假阳性无防护。本次补齐：
  - `material_add_expression`：回读确认表达式真的出现在材质表达式清单中；
  - `material_connect_expressions`：回读目标输入引脚实际连到的表达式并比对，
    另检测「连线后新增表达式」= 引擎插入的隐式类型转换节点（P04 同类信号）；
  - `material_connect_property`：回读材质主属性（`base_color` 等）实际连到的表达式。
  降级口径对齐 `_pywrap_readback_nodes` 的 R1 策略：**能读到就严格比对，读不到
  只记 `warnings` 不阻断**（避免非 GameThread 等场景制造假阴性）。返回体新增
  `verified` 字段，使"验证没发生"成为可被 agent 检查的一等信号。
  ⚠️ **语义边界**：`verified` 与 `warnings` **解耦**——它只回答"回读比对是否
  执行且通过"，**不因非致命告警变 false**（例：检测到引擎插入的隐式转换节点时，
  连线本身已按引脚比对通过 → `verified=true` + 一条 warning）。比对读不到时
  才 `verified=false` + 告警。
- **`widget_add_child` 补回读**：用 `read_widget_tree` 确认控件真的进了树。
  同族 `widget_set_property` 早有 `ReadWidgetProperty` 回读，本条原先缺失 →
  族内验证密度不一致。
- **`material_compile` 修 `is False` 漏判**：原写法 `if ok is False` 会放过
  `None`（引擎 API 失败路径返回 None 是常态）→ 把未编译报成 `compiled:True`。
  改为与全项目一致的 `if not ok`，错误文本附实收值。
- **`compile_blueprint` 补 `success` 键（MCP `isError` 漏点修复）**：
  MCP 层判据是 `result.get("success") is False`，而该命令只报 `compiled:false`
  → **编译失败会以 `isError:false` 返回客户端**，agent 据 `isError` 判定时会当成功
  （ibrews 手册 §9.4「Treat isError as load-bearing」即此约定）。现补 `success`。
- **MCP 层 `is_err` 同时认 `success` 与 `ok`**：蓝图节点/变量/组件族共 36 条写命令
  的返回体只有 `ok`（`_pywrap_ctor_result` / `_pywrap_node_result`），原先只判
  `success` → 这些命令一旦改成「返回 `ok:False` 而非 raise」就会静默假成功。
  当前它们靠 raise 兜住，本次把这条隐式契约显式化。
- **测试基础设施：`fake_unreal.py` 补材质/控件族替身**（原先 `grep -ci material` = 0）
  → 材质族此前**零离线用例**，这是「验证密度低于蓝图族」的直接原因。新增：
  `FakeMaterial` / `FakeMaterialExpression` / `FakeExpressionInput` /
  `MaterialEditingLibrary` / `MaterialProperty` / `FakeWidgetBlueprint`，
  以及 8 个故障注入开关（`set_mat_connect_false_positive` 复现 P04 类假阳性、
  `set_mat_add_expression_drop`、`set_mat_insert_converter`、
  `set_mat_readback_unavailable` / `set_mat_verify_unavailable`、
  `set_mat_compile_result`、`set_mat_widget_add_drop` / `set_mat_widget_prop_drop`）。
  顺带修 `FakeClass.get_path_name()` 缺失（真实 UClass 有）。
- 新增 `tests/test_mat_readback.py`：**25 个用例**，覆盖正路径 / 假阳性 fail-loud /
  隐式转换告警 / 回读降级（连线与主属性两路）/ `verified` 语义边界回归 /
  `None` 漏判回归 / `success` 键回归 / MCP `isError` 契约。
- 离线回归：**504 通过 / 0 失败**（95 跳过 = 需活体编辑器的在线用例；基线 479）。

## [3.3] — 2026-09-22（skill v0.19）
- **MCP 工具目录规范化（面向 AI 客户端的工具选择质量）**：
  - 命名统一为 `ue5_<verb>_<noun>`：`ue5_health→ue5_check_health`、`ue5_diag→ue5_run_diagnostics`、
    `ue5_compile→ue5_compile_blueprint`、`ue5_logs→ue5_read_logs`、`ue5_batch→ue5_run_batch`、
    `ue5_command→ue5_execute_command`、PIE 6 个 → `ue5_start_pie` / `ue5_stop_pie` /
    `ue5_read_pie_state` / `ue5_list_pie_actors` / `ue5_read_pie_property` / `ue5_set_pie_transform`。
  - 23 个工具描述重写为「英文主 + 中文注」，含 Use-when 指引与返回形态；`ue5_execute_command`
    明确标注为"专用工具未覆盖时使用"的兜底入口。
  - 参数 schema 逐项补 description（原多数为空）。
  - 工具数仍为 23；内部 72 条命令白名单与 CLI 命令名不受影响。
- 离线回归：**503 通过 / 0 失败**（95 跳过 = 需活体编辑器的在线用例）。
- 已知遗留：UE 5.8 官方-MCP 工具集（`bridge/ue58-extras/`，8 工具）命名暂未同步，留待下版对齐；
  `manifest.json` 为打包期生成产物，MD5 清单待下次打包时再生成。

## [3.2] — 2026-09-18（skill v0.18）
- **UE 5.8 全链路打通**：C++ 桥按 5.8 编译（MSVC 14.44.35207）并通过官方 MCP 端到端验收
  （88 个桥方法、UMG 支持可见）。
- **预编译 5.8 桥随包**：`bridge/prebuilt-5.8/`（DLL + UnrealEditor.modules）→ 5.8 零编译。
- `build_for_engine.bat` 修复：自动同步 `UnrealEditor.modules`（缺失会被 UE 拒载）。
- 源码 5.8 兼容修复：`DoesPackageExist`/`SavePackage` 统一现代签名（5.1/5.8 单套代码）。
- `ue58-extras` 工具集加固：工具返回 JSON 字符串（`dict` 返回不被注册器支持）；
  可选字符串参数使用 `"*"` 哨兵默认（空串默认会被 schema 生成器视为必填）。
- 文档：BUILD.md / UE_VERSION_GUIDE.md 更新 5.8 工具链要求（MSVC ≥ 14.44 且 VS2022 ≥ 17.14）。
- **P0 崩溃修复（前置守卫 GameThread 分流）**：`/Game/` 内容类路径的
  `load_class`/`load_object` 探测改投 GameThread 执行——非 GameThread 触发的
  `LoadPackage` 会命中 `IsInGameThread()` 断言使编辑器直接退出（实测复现两次）；
  `/Script/` 引擎路径与离线测试维持调用线程直呼。全量回归 478 通过 / 0 失败。

## [3.1] — 2026-09-18（skill v0.17）
- **UMG / 材质写操作回读比对**：设置后强制读回校验，不一致即 fail-loud。
- 新增 C++ `ReadWidgetProperty`；共享 `_value_matches()` 值比对助手
  （浮点容差 / 结构体导出格式差 / 引号差异）。
- **FSlateColor 假成功修复**（回读机制首个实战战果）：
  `(R=..,G=..,B=..,A=..)` 会被静默接受但值不变 → 自动按
  `(SpecifiedColor=(...))` 重试并回读确认。

## [3.0] — 2026-09-18（skill v0.16）
- **MCP 协议接入层**：`/mcp` 端点（零依赖手写，兼容 UE5.1 内嵌 Python 3.9）。
- **PIE 运行时验证族**：`pie_start/stop/state` + 运行时对象枚举/属性读取/传送回读。
- **Actor 基础族**：`find_actors` / `spawn_actor`（可选 mobility）/
  `set_actor_transform` / `get_actor_properties`。
- **日志自取**：`logs_read`（游标增量 + 过滤）。
- **批量执行**：`editor_batch`（命令序列单次请求）。
- **C++ 桥源码随包** + `build_for_engine.bat` 一键重编译（5.2–5.8 自助）。

## [2.20] — 2026-09-18（skill v0.15）
- 独立验证轮：修复 13 项缺陷（E-1~E-13）——诊断引擎默认调用崩溃、
  Windows 图渲染崩溃、编译失败谎报成功、disconnect/delete 假成功、
  5.2–5.5 误部署拦截、冷启动 token 竞态等。
- 文档全库口径统一；泄漏扫描零命中。

## [2.19] — 2026-09-17（skill v0.14）
- 蓝图缺陷族 D-1~D-8：事件存在性误判（静默漏建）、同名节点 GUID 定位、
  `add_function_node` 回传 GUID、`delete_asset` 保存作用域收窄、
  `/build` events/functions 字段契约。
