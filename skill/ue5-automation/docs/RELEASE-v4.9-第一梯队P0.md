# v4.9 · 第一梯队 P0 四项落地说明

> 2026-10-28 · 依据 `COMPETITIVE-v4.8-功能维度效率美观.md` 的结论
> 范围：上下文治理 / 会话保活 / 一键接入 / 行为验证
> 原则：**加法，不减能力**——工具目录保持全量，27→…→32 个工具全部保留。

---

## 1. P0-1 上下文治理（加法）

**问题**：32 个工具的扁平目录 + `execute_command` 描述里塞 86 条命令名，
常驻成本约 22.1k 字符（≈6.9k token），且发现方式只有"读描述"。

**做法**：
- 新增 MCP 工具 `ue5_tool_search{query, limit, include_tools}`：按关键词检索
  87 条白名单命令 + 32 个 MCP 工具，返回命令名、参数、一句话说明；
  中文说明与英文命令名都能命中（如 `material` / `撤销`→回滚）。
- `ue5_execute_command` 描述从 2,347 → **533 字符**：去掉 86 条命令名清单，
  改为"用 `ue5_tool_search` 查命令名"。
- 顺带压缩截图/整图/任务三个冗长描述（保留关键纪律与交叉引用）。

**结果**：32 工具（含新增）描述总长 22.1k → **19.8k 字符（≈6.2k token）**；
能力零删减；`UE5_MCP_CORE_TOOLS=1` 仍可收窄到 17 个。

**未做**（P1）：namespace 分派、`_fields` 响应塑形——先把"按需发现"跑通。

---

## 2. P0-2 会话保活（编辑器重启不掉会话）

**问题**：stdio 客户端（Claude Desktop 等）把代理当长连接；编辑器重启 →
桥离线 → 客户端整个会话失效，用户要重开客户端。

**做法**（`ue5_mcp_stdio.py`）：
- `tools/call` 遇到 ConnectionError → 轮询 `/health` 等待桥恢复
  （默认 **30s**，`UE5_BRIDGE_RECONNECT_S` 可调），恢复后**重发一次**；
- 等待期间重读 token（编辑器重启可能重新生成）；
- 超时后返回可读错误（含等待秒数与调参说明），绝不假成功；
- 只对 ConnectionError 重试；Timeout 不重试（避免重复执行写操作）。

**验证**：`test_mcp_stdio_proxy.py` 新增"桥后起 → 自动恢复"用例
（真实子进程 + 延迟启动的 HTTP 假桥），4/4 通过。

---

## 3. P0-3 一键接入

**问题**：原 `install_mcp.py` 会向所有已知客户端写配置（含没装的），
没有 ZCode 目标，默认直接落盘。

**做法**：
- 新增 **ZCode** 目标（`~/.zcode/cli/config.json` → `mcp.servers`）；
- **只写检测到的客户端**（配置文件或目录存在）；`--all` 强制全写；
- 新增 `--dry-run`（默认输出计划，不落盘）；`--list` 显示 `detected=yes/no`；
- 幂等（重复运行不重复条目）、写前备份、保留其他键；
- Python 3.9 兼容（去掉 `X | None` 语法）。

**验证**：新增 `tests/test_install_mcp.py` 5 项（ZCode 形态/保留键/检测/
dry-run 不落盘/幂等与备份）；本机 `--dry-run` 检测到 ZCode + Claude Code。

---

## 4. P0-4 行为验证（语义验证）

**问题**：编译通过、图与 spec 一致、截图有变化——都证明不了"行为正确"。
全赛道都缺这一层，是第一梯队也没做透的空白。

**做法**：新增白名单命令 `verify_behavior`（87 条）：

```json
{"command": "verify_behavior", "params": {
  "actor_name": "BP_Door_1", "property_name": "state",
  "expected": "Open", "match": "exact",
  "world": "pie", "capture": true}}
```

- `world="pie"` 默认：PIE/Simulate 未运行 → fail-loud + `pie_start` 指引；
- `match`: `exact`（数值支持 `tolerance`）| `contains`（忽略大小写）；
- `verified` 是唯一判据；断言失败 → `success=false` + `_fail_loud`；
- `capture=true` → 截图证据；**截图失败不翻转 verified**，只进 `warnings`；
- 回读值是 JSON 形态（字符串带引号）→ 自动解包，`expected` 带不带引号都等价；
- 结束时清理 PIE（调用方负责 `pie_stop`）。

**验证**：`tests/test_verify_behavior.py` 10 项；双引擎真机各 8 步全过：

| 项 | 5.1（simulate） | 5.8（simulate） |
|---|---|---|
| `tools/list` = 32 + tool_search 命中 material | ✅ | ✅ |
| verify 通过（属性 + 截图落盘） | ✅ | ✅ |
| verify 失败（伪造期望）→ fail-loud | ✅ | ✅ |
| PIE 未运行 → 可读错误 | ✅ | ✅ |
| install_mcp --dry-run | ✅ | ✅ |

---

## 5. 数据对比（v4.8 → v4.9）

| 指标 | v4.8 | v4.9 |
|---|---|---|
| MCP 工具 | 31 | **32**（+tool_search） |
| 白名单命令 | 86 | **87**（+verify_behavior） |
| 工具描述体量 | 22.1k 字符 / ≈6.9k token | **19.8k / ≈6.2k** |
| `execute_command` 描述 | 2,347 字符 | **533** |
| 编辑器重启后 stdio 会话 | 断 | **自动等待恢复** |
| 客户端接入 | 手写/全量写 | **检测 + dry-run + ZCode** |
| 行为验证 | 无 | **verify_behavior** |
| 测试 | 727 | **743** |

---

## 6. 已知边界与下一步

- `verify_behavior` 只能读**可反射属性**；没有可读属性的状态，改用日志/计数器断言。
- 截图失败不翻转断言（证据失败 ≠ 行为失败），但会在 `warnings` 明示。
- 会话保活只覆盖"桥短暂离线"；客户端进程自身被重启不在范围内。
- P1 未做：namespace 分派/响应塑形、AnimBP 或 Niagara 纵深、资产级回滚、
  DataTable 批量、打包钩子——按 `COMPETITIVE-v4.8` 的顺序继续。
