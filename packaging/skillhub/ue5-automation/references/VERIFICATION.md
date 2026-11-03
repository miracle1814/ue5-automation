# 验证记录与已知边界

## 版本对应

| 项 | 版本 |
|---|---|
| 本 Skill 包 | **6.5.11**（2026-11-03） |
| 运行包 | **6.5.5**（2026-11-02） |
| 本轮真机 | UE 5.1.1 |
| 历史真机（非本轮） | UE 5.1.1 / UE 5.8.2 |

> 6.5.11 只改 Skill 包内的自检脚本与文档，运行包仍是 6.5.5。
> 下面的「本轮验证」是 6.5.11 实际跑过的结果；「历史记录」只用于追溯，
> 不作为 6.5.11 的验证依据。

## 本轮验证（6.5.11，2026-11-03）

### 离线回归

```text
852 passed / 95 skipped / 0 failed
```

新增 6 条脚本回归：两种 health 结构归一化、完整 command 信封、缺失字段、
HTTP 500 重试后成功、401 不重试且带结构化错误。

### 本轮真机

环境：Windows + UE **5.1.1-23901901**，引擎自带 Python 3.9.7，
运行包 **v6.5.5**，`TP_BlankBP` 模板工程。

| 步骤 | 结果 |
|---|---|
| `doctor.py --retries 3` | token / health / `bridge_loaded` / 版本 / 项目路径全 PASS |
| `smoke.py --retries 3`（连续两次） | 全 PASS：建图 → 搜索 → 写图 → 编译 → 读回 → 保存 |
| 重复运行 | 已存在 `BP_Smoke` 时删除重建，第二次仍全 PASS |
| `doctor.py --json` | `ok=true`，每个 check 带 `ok` 字段 |

### 本轮修复的评测问题

| 评测反馈 | 6.5.11 处理 |
|---|---|
| doctor / smoke 对 health 字段假设不一致 | 新增 `scripts/bridge_client.py`，统一 `normalize_health()`；两者走同一客户端 |
| 网络波动没有重试 | 默认 `--retries 2`，仅重试连接失败与 HTTP 5xx |
| 错误缺少分类 | 统一输出 `code` / `message` / `hint` / `http_status`；401/403 立即失败 |
| 验证依据沿用旧版本 | 本轮验证与历史记录分节；历史 5.8 数据明确标注“非本轮” |

## 6.5.10 历史验证（2026-11-02，运行包 6.5.5）

| 步骤 | 结果 |
|---|---|
| `doctor.py` | 全 PASS |
| `smoke.py`（连续两次） | 全 PASS |
| `search_blueprint_nodes` | `BeginPlay` 命中 `ReceiveBeginPlay`；`PrintString` 命中内置目录；`print` 不再返回 `Blueprint*` |
| 重复启动 | 仅一条 `service started`，不再出现 10048 |
| `spawn_actor(world="pie")` | UE 5.1 Python 未导出生成接口 → 明确报错并给出替代路径 |

## 历史矩阵（6.5.4，非本轮）

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

## 验证纪律

- 每个写操作读回比对，失败 `_fail_loud`；
- 编译走结构化诊断，不走日志正则猜；
- 场景 `verified` 只看断言/命令，截图失败只算证据失败；
- AnimBP 读不到节点时明确降级，不宣称节点可读；
- 移植包有逐文件 SHA256 清单，可跨机对比。

## 已知边界

- Windows only；
- UE 5.2–5.7 需自行重编译 bridge；
- UE 5.1 的 Python 未导出 PIE 动态生成入口；需先放编辑器世界或预置 Actor；
- 会话锁是协作式，不是编辑器强制锁；
- AnimBP 只读，写入未开放；
- 无 TLS；跨机使用需自建隧道；
- 复杂 PIE 场景请先在工程内小步试，再放进 `verify_scenario`。

## 未重跑的部分（如实说明）

- 6.5.11 未重跑 UE 5.8 真机；上表 5.8 数据是 6.5.4 历史记录；
- 未新增 5.2–5.7 真机记录（这些版本仍需自行重编译 bridge）；
- 未做跨机/跨网络（非 localhost）验证；bridge 无 TLS。
