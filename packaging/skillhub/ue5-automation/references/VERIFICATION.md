# 验证记录与已知边界

## 版本对应

| 项 | 版本 |
|---|---|
| 本 Skill 包 | **6.5.10**（2026-11-02） |
| 运行包 | **6.5.5**（2026-11-02） |
| 真机验证 | UE 5.1.1 + UE 5.8.2 |

> 6.5.10 对应运行包 6.5.5：修复节点搜索、重复启动与 PIE 生成报错，并新增
> `doctor.py` / `smoke.py`。下面的 2026-11-02 数据为本版重跑；2026-10-29 的
> 记录保留作历史对照。

## 离线回归（运行包 6.5.5）

```text
846 passed / 95 skipped / 0 failed
```

- `95 skipped` 是需要真实编辑器的在线用例，不是失败；
- 离线覆盖：命令分发、参数校验、路径边界、幂等/锁、场景步骤、错误分类、
  fail-loud 分支；
- 移植包提供逐文件 SHA256 清单，可跨机比对。

## 真机矩阵（6.5.4 历史记录）

| 项 | UE 5.1.1 | UE 5.8.2 |
|---|---|---|
| bridge 启动 / health | 通过 | 通过 |
| 会话锁冲突/释放 | 通过 | 通过 |
| 幂等重放/跨命令冲突 | 通过 | 通过 |
| 场景 command 步骤 | 通过 | 通过 |
| 场景高危命令拦截 | — | 通过 |
| AnimBP probe | 非 AnimBP 如实报 false | 真实 AnimBP 可读 |
| AnimBP read | 非 AnimBP fail-loud | 枚举 15 图；节点级未暴露，降级告警 |
| HTTP 413（9MB） | — | 通过 |
| stdio 34 工具 | — | 通过 |

## 本次实机冒烟（2026-11-02，运行包 6.5.5）

环境：Windows + UE **5.1.1-23901901**，引擎自带 Python 3.9.7，
运行包 **v6.5.5**，全新 `TP_BlankBP` 模板工程（非真实项目）。

| 步骤 | 结果 |
|---|---|
| `scripts/doctor.py` | token / health / `bridge_loaded` / 版本 / 项目路径全 PASS |
| `scripts/smoke.py`（连续两次） | 全 PASS：建图 → 搜索 → 写图 → 编译 → 读回 → 保存 |
| `search_blueprint_nodes` | `BeginPlay` 命中 `ReceiveBeginPlay`；`PrintString` 命中内置目录；`print` 不再返回 `Blueprint*` |
| 重复启动 | 仅一条 `service started`，不再出现 10048 双自启 |
| `spawn_actor(world="pie")` | UE 5.1 Python 未导出生成接口 → 明确报错并提示编辑器世界/预置 Actor 两条替代路径 |

已知边界：

- UE 5.1 的 `BeginSpawningActorFromClass` / `BeginDeferredActorSpawnFromClass`
  标记 `BlueprintInternalUseOnly`，Python 侧没有生成入口；本版不宣称 5.1 可在
  PIE 内动态生成 Actor。
- 本轮未重跑 5.8 真机与截图场景；5.8 数据沿用 6.5.4 记录。

## 历史实机冒烟（2026-10-29，运行包 6.5.4）

环境：Windows + UE **5.1.1-23901901**，引擎自带 Python 3.9.7，
运行包 **v6.5.4**，全新 `TP_BlankBP` 模板工程（非真实项目）。

| 步骤 | 结果 |
|---|---|
| `health` | `bridge_loaded=true`，engine 5.1.1 |
| `build_blueprint BP_Smoke` | `success=true` |
| `search_blueprint_nodes` | `BeginPlay` 命中事件候选；`PrintString` / `Delay` 未命中（引擎函数库不在 Python 包装内） |
| `apply_graph` BeginPlay→PrintString | 2 节点 / 1 连线，`verified=true`，`compiled=true` |
| `compile_and_diagnose` | `structured=true`，`compiled=true`，0 错误 |
| `get_pin_default_value` | `"Hello from skill test"`，`matched_by=guid` |
| `save_asset` | `saved=true`，22,749 bytes |
| `read_blueprint` L2 | 2 节点 / 1 连线；标题本地化（`事件开始运行` / `打印字符串`） |
| `verify_behavior` | `bHidden=false` → `verified=true`（`"False"` 因大小写失败，已记录） |
| `verify_scenario` | wait + log_contains，`verified=true`，JUnit 已写 |
| 任务账本 / 锁 / 撤销状态 | `task_begin/end`、`session_lock_*`、`agent_undo_status` 均可用 |

已知现象（不影响主路径）：

- 两个自启入口会各调一次 `start()`，第二个打印 `[Errno 10048] ... service stopped`；
  `/health` 正常，服务由第一个入口提供（**6.5.5 已修复**）。
- `spawn_actor` 直接往 PIE 世界生成蓝图 Actor 返回 None；先放编辑器世界再 PIE 可正常
  BeginPlay（本次即用此路径验证）。
- 未采集实机截图（窗口失焦会导致 PIE 截图超时）；行为验证不依赖截图。

## 验证纪律

- 每个写操作读回比对，失败 `_fail_loud`；
- 编译走结构化诊断，不走日志正则猜；
- 场景 `verified` 只看断言/命令，截图失败只算证据失败；
- AnimBP 读不到节点时明确降级，不宣称节点可读；
- 移植包有逐文件 SHA256 清单，可跨机对比。

## 本次发布做了什么

- 运行包 6.5.5：`search_blueprint_nodes` 增加 `PrintString` / `Delay` 静态目录，
  修正 `BeginPlay` → `ReceiveBeginPlay` 匹配，过滤 `Print` → `Blueprint*` 噪声；
- 运行包 6.5.5：`start()` 增加跨模块启动锁与 5 秒端口宽限，消除重复加载的
  10048 双自启；
- 运行包 6.5.5：PIE `spawn_actor` 在 UE 5.1 Python 无生成入口时明确报错，
  不再静默返回 `None`；
- Skill 6.5.10：新增 `scripts/doctor.py` / `scripts/smoke.py`，smoke 支持重复运行；
- 文档同步：安装日期、版本表、验证记录与已知边界按 6.5.5 实机结果更新。

历史文档修订（6.5.9）：元数据 `requires_api_key: false`；新增 `PITFALLS.md` /
`EXAMPLES.md`；`SKILL.md` 增加「何时不要用」「工具档位」；统一版本口径；
命令目录改为以 `health.mcp_tool_count` / `tool_search` 为准。

## 没重跑的部分（如实说明）

- 本版未重跑完整 PIE 属性/截图场景，沿用 6.5.2–6.5.4 记录；
- 未新增 5.2–5.7 的真机记录（这些版本仍需自行重编译 bridge）；
- 未做跨机/跨网络（非 localhost）验证；bridge 无 TLS。

## 已知边界

- Windows only；
- UE 5.2–5.7 需自行重编译 bridge；
- 会话锁是协作式，不是编辑器强制锁；
- AnimBP 只读，写入未开放；
- 无 TLS；跨机使用需自建隧道；
- 复杂 PIE 场景请先在工程内小步试，再放进 `verify_scenario`。
