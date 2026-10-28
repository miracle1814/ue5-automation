# v6.4 安全与稳定性审计（排漏洞 / 修 Bug / 回归）

> 日期：2026-10-28 · 范围：v6.3 全部代码（不含新增功能）
> 目标：完整性、回归性、稳定性过关；能力不扩张，只做硬化。

---

## 1. 发现并修复的问题

| # | 级别 | 问题 | 修复 |
|---|---|---|---|
| 1 | 🔴 高危 | **资产路径目录逃逸**：`/Game/../...` 可让 `_asset_stat` 哈希任意文件、`_snapshot_asset_file` 写到快照目录之外 | 新增 `_safe_game_pkg`：拒绝 `..`/`.`/反斜杠/非 `/Game/`；`_asset_stat`、`_snapshot_asset_file`、批量操作、DataTable 全部走它 |
| 2 | 🔴 高危 | **`logs_read.log_file` 任意文件读取**：可读 `%APPDATA%/ue5_bridge/token` 等任意文件 | `_resolve_log_file` 只允许：项目日志目录、项目 `Saved/Logs`、系统临时目录；越界返回“日志文件不可定位” |
| 3 | 🔴 高危 | **`verify_scenario.junit_path` 任意文件写入**：可覆盖 `C:\Windows\...` | JUnit 输出白名单：项目 `Saved/ue5-automation/scenarios`、`%APPDATA%/ue5_bridge`、系统临时目录；越界 `path_escape` |
| 4 | 🟠 中危 | **资产创建/保存可指向 `/Engine`**：`save_asset`/`build_blueprint`/`material`/`widget`/`datatable` 未限制项目内容 | 统一 `_safe_game_pkg` 校验；`delete_asset` 与 `save_asset` 只允许 `/Game/` |
| 5 | 🟠 中危 | **CSV 无大小上限**：`datatable_write_csv` 可被超大字符串拖垮内存 | 5MB 硬上限 |
| 6 | 🟠 中危 | **`verify_scenario.log_contains` 正则 ReDoS** | 改为字面量匹配（`re.escape`）+ pattern ≤200 字符 |
| 7 | 🟡 低 | **adhoc 资产锁不释放**：无任务的写命令持锁 300s，阻塞后续任务 | 锁只由任务持有；adhoc 不加锁（单进程 GT 串行化）；`batch_asset_ops` 对每个 from/to 加锁 |
| 8 | 🟡 低 | `task_end` 异常时锁不释放 | `finally` 释放该任务全部锁 |
| 9 | 🟡 低 | `ue5_execute_command{command:"verify_scenario"}` 在 GT 上跑 → 等待阻塞 PIE | MCP 层特判走 HTTP 线程（与 `ue5_verify_scenario` 同路径） |
| 10 | 🟡 低 | `logs_read` pattern 无长度上限 | 200 字符上限 |

---

## 2. 验证

### 离线

- **795 passed / 0 failed / 95 skipped**（新增安全测试 10 项）；
- `python -m compileall` 无语法错误；
- 关键新增测试：
  - 路径穿越（`/Game/../`、`.`、反斜杠、`/Engine/`）全部拒绝；
  - `logs_read` 读项目外文件被拒；
  - `junit_path` 越界被拒；
  - CSV >5MB 被拒；
  - 日志 pattern 传 `a.b` → 实际按 `a\.b` 字面量；
  - 批量操作对 4 个资产加锁；
  - adhoc 不加锁；任务锁在 `task_end` 释放。

### 真机

| 检查 | 5.1 | 5.8 |
|---|---|---|
| live_smoke（health/diag/34 tools/ui） | ✅ | ✅（97 方法） |
| `save_asset /Engine/...` 拒绝 | ✅ | ✅ |
| `delete_asset /Engine/...`（confirm）拒绝 | ✅ | ✅ |
| `logs_read` token 文件拒绝 | ✅ | — |
| `batch_asset_ops /Game/../` 拒绝 | ✅ | — |
| `verify_scenario` JUnit 越界拒绝 | ✅ | — |
| 正常 health/读写/回执/锁 | ✅ | ✅ |

---

## 3. 仍存在的风险（如实记录）

1. **`logs_read` 仍是用户正则**（≤200 字符、拒绝嵌套量词 `(a+)+` 类形态）：已做输入长度与回溯炸弹形态预检，仍非完全沙箱。
2. **锁是进程内锁**：单编辑器实例有效，不是跨机器分布式锁。
3. **`/ui` 静态壳公开**：HTML 不含数据；所有数据接口仍要求 token。token 存在 localStorage，本机可读。
4. **token URL**：v6.4.1 起面板 API 改用 `X-Skill-Token` 头，`/ui` 可不带 token 打开；截图 `<img>` 仍用查询参数（浏览器无法给 img 加头），该 URL 不进地址栏。
5. **无 HTTP 速率限制**：仅监听 127.0.0.1，单机场景接受。
6. **回执是 sha1 文件指纹**，不是签名；不能防篡改，只证明“文件变没变”。

---

## 4. 稳定性

- 全量测试 795 项通过；双引擎各重启多次后 live_smoke 均通过。
- 任务账本清理后 8 条真实任务；pytest 已隔离（不会污染生产账本）。
- 锁/回执在任务内多次写入、保存、`task_end` 后状态一致（真机验证）。
