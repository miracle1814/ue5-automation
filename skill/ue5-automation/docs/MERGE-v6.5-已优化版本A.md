# 已优化版本 A 合并报告（v6.5）

> 来源：`已优化版本A/UETool-v6.2.1-完整交付.zip`（UETool v6.2.1-dev，
> skill v2.0.1，基线 v6.0.2 `ccd5cea`）。
> 目标：与本仓库 v6.4.1（基线 `ccd5cea` + v4.5–v6.4.1 未提交工作）对比，
> 只吸收增量能力，不覆盖我们已有的硬化/UI/场景/回执实现。

---

## 1. 结论先说

- 外部包是 **v6.0.2 的分支**，不是我们 v6.4.1 的延续；整包替换会丢掉
  v6.3–v6.4.1 的回执、资产锁、场景 JUnit、路径/日志/JUnit 安全收紧、
  面板重做与测试隔离。因此采用 **逐项合并**，不做文件级覆盖。
- 采纳 9 组增量：幂等键、显式会话锁、回执查询、AnimBP 只读、场景安全、
  HTTP 体积上限、stdio 去 requests、token 去默认值、发布自检/CI。
- 合并后：**101 条白名单命令、34 个 MCP 工具、gateway 12 / core 18 / full 34**；
  离线全量 **820 passed / 95 skipped / 0 failed**。

---

## 2. 对比矩阵

| 能力 | 外部 v6.2.1-dev | 我们 v6.4.1 | 本次处理 |
|---|---|---|---|
| 幂等键 | key → 结果缓存；命令+参数指纹冲突 fail-loud；TTL 600s | 无 | **采纳**，放进 `_dispatch_args_with_tx`，覆盖 `/command`、`execute_command`、build/apply、batch/scenario 内层 |
| 显式会话锁 | `session_lock_acquire/release/status`，TTL/上限/force | 只有 task 自动锁 | **合并**：新增显式锁，并与任务锁**互相可见**；任务锁不能被 force 抢占 |
| 回执 | 200 条内存回执，`receipt_list` | 任务账本 + before/after sha1 | **合并**：保留我们的指纹回执，新增 `receipt_id`、内存环形表与 `receipt_list` |
| AnimBP | `animbp_probe/read` 只读，fail-loud | 无 | **采纳**，写入通道继续关闭 |
| 场景 | action 语法 + command 步骤 + 高危拦截 + 总超时 | type 语法 + assert/log/截图 + JUnit | **合并**：兼容两种语法，新增 command/pie_start/pie_stop/总超时/高危拦截；保留我们的 JUnit 路径白名单 |
| HTTP 体积 | 8MB Content-Length 上限 | 无 | **采纳**（`UE5_MAX_REQUEST_BYTES`） |
| stdio 代理 | urllib，零 requests | requests | **采纳**，保留重连/token 重读逻辑 |
| token | 缺失返回空串 | 回退 `test123` | **采纳**，并补上“桥启动后重读 token、403 刷新重试” |
| 工具面 | gateway 11 / core 18 / full 33；100 命令 | gateway 5 / core 18 / full 34；95 命令 | **改造**：gateway 扩到 12（含我们独有的 verify_behavior），保留 34 工具；新增 `UE5_MCP_PROFILE`，兼容旧布尔开关 |
| 发布自检 | verify_release/CI/交叉验证文档 | 分散的脚本与人工验证 | **改造**：`packaging/portable/verify_release.py` 适配仓库/移植包两种布局；不把“未真机验证”写成已发布 |
| 安全修复 | 日志越界/引擎资产/CSV/NaN 等 | v6.4 已覆盖更严 | **保留我们的实现**，只补 `UE5_LOG_ALLOW_PATHS` 与输入上限 |
| 面板 | 无我们 v6.1 的面板 | 只读管理页 | **不采纳**外部任何 UI 变更；面板继续只读 |

---

## 3. 明确拒绝/未采纳

1. **整包覆盖**：外部基线 v6.0.2，覆盖会回退 v6.3–v6.4.1 的 800+ 测试、
   回执/锁、场景、安全收紧与面板。
2. **外部“已完成”口径**：外部 `SECURITY-HARDENING` 明确写“未做 UE 真机回归”；
   我们不把它当作已验证事实，真机结论只以本仓库 5.1/5.8 实测为准。
3. **gateway=11 的精确数字**：我们保留 `ue5_verify_behavior`（外部没有），
   gateway 为 12；这是能力差异，不是回归。
4. **force 抢占任务锁**：force 只抢占其他会话的显式锁；任务锁必须等
   `task_end`，避免一个 agent 把另一个正在执行的任务锁抢走。
5. **外部 2MB CSV 上限**：我们 v6.4 已是 5MB；不收紧到更小，避免破坏既有用法。
6. **外部日志 pattern 256 字符**：我们保持 200 字符（更严）+ 嵌套量词预检。

---

## 4. 合并后的行为约定

### 4.1 幂等键

```json
{"command": "apply_graph",
 "params": {"bp_path": "/Game/BP_X", "graph_spec": {...}},
 "idempotency_key": "agentA-001"}
```

- 同一 key + 完全相同 command/params，10 分钟内直接重放上次**成功**结果，
  返回体带 `idempotent_replay: true`；
- key 被复用到不同 command/params → `idempotency_conflict` fail-loud；
- 失败/`_fail_loud` 结果不缓存；>256KB 结果不缓存（防截图/大图放大内存）。

### 4.2 会话锁 vs 任务锁

| | 任务锁（v6.3） | 会话锁（v6.5） |
|---|---|---|
| 谁持有 | `task_begin` 后的当前 task_id | 调用方显式 `owner` |
| 何时获取 | 写命令回执入口自动 | `session_lock_acquire` 显式 |
| TTL | 300s（`UE5_ASSET_LOCK_TTL`） | 600s（可传 5–86400s） |
| 释放 | `task_end` 自动 | `session_lock_release` / 过期 |
| force | 不支持 | 可抢占其他**会话**锁，不抢任务锁 |

- 带 `session_id` 的写命令先检查两种锁；
- task 写命令的 `_receipt_begin` 也会检查会话锁；
- 面板 `locks` 同时展示 `scope: task|session`。

### 4.3 场景

- `type` / `action` 两种写法等价；`assert_property`→`assert`，
  `assert_log`→`log_contains`；
- 新增 `command`、`pie_start`、`pie_stop` 步骤；
- `command` 默认拒绝 `delete_asset`、`agent_undo`、`batch_asset_ops`、
  `apply_graph{replace}`、`datatable_*`、`rollback_task_assets` 等高危命令；
- 禁止场景内递归 `verify_scenario` 和 `editor_batch`（防绕过高危检查）；
- 总超时默认 300s（上限 1800s），超时步骤记为 `timeout` 失败并跳过剩余。

---

## 5. 验证

- `python -m pytest -q skill/ue5-automation/tests`
  → **820 passed / 95 skipped / 0 failed**（含新增 23 项回归：12 多 Agent/AnimBP + 5 stdio + 6 场景）。
- `python packaging/portable/verify_release.py --json`
  → version / tool_profiles / security_invariants 全过（版本号在打包时更新）。
- 新增测试：`test_merge_v65.py`（11 项）、`test_stdio_urllib.py`（5 项）、
  `test_verify_scenario.py` 新增 6 项。
- 真机：见 `docs/CROSS-VALIDATION-v6.5.md`（UE 5.1 + 5.8）。
