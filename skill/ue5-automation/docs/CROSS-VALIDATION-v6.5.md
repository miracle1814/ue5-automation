# 跨引擎验证记录（v6.5）

> 日期：2026-10-28 · 版本：v6.5.0 / skill v1.14.0
> 工程名与路径已脱敏；token 不入文档、不入包。

## 0. 验证范围

| 项 | UE 5.1.1 | UE 5.8.2 |
|---|---|---|
| bridge 启动 / health | ✅ | ✅ |
| 白名单计数 101 / 工具 34 | ✅ | ✅ |
| 显式会话锁 acquire/conflict/release | ✅ | ✅ |
| 幂等键重放（`idempotent_replay`） | ✅ | ✅ |
| 幂等键跨命令冲突 | — | ✅ |
| 场景 `command` 步骤（未启 PIE） | ✅ | ✅ |
| 场景高危命令默认拒绝 | — | ✅ |
| AnimBP `probe` | ✅（非 AnimBP 资产，如实报 false） | ✅（真实 AnimBP，`get_animation_graphs`=true） |
| AnimBP `read` | ✅（非 AnimBP → fail-loud） | ✅（枚举 15 张图；节点级属性未暴露 → 降级告警，不假成功） |
| HTTP 413（9MB Content-Length） | — | ✅ |
| stdio 代理 tools/list + 工具调用 | — | ✅（34 工具，零 requests） |

“—”表示该轮未跑；不是失败项。

## 1. 离线基线

```text
python -m pytest -q skill/ue5-automation/tests
820 passed, 95 skipped, 0 failed, 27 subtests passed

python packaging/portable/verify_release.py
OK version / tool_profiles / security_invariants
```

- profile：gateway 12 / core 18 / full 34；命令 101。
- 安全不变量：日志越界拒绝、幂等冲突、引擎资产写拒绝、场景高危默认拒绝、
  HTTP 上限、token 无默认值。

## 2. UE 5.8 实录

1. 更新部署后 `health`：`status=ok`，`engine_version=5.8.2-56702186+++UE5+Release-5.8`。
2. `session_lock_acquire(owner=A, /Game/...)` → 成功；
   `owner=B` 同资产 → `资产被其他会话锁定 ... 剩余 59s`；
   `session_lock_status` → 1 条；`release(A)` → released=1。
3. `health` + `idempotency_key` 第二次 → `idempotent_replay: true`；
   同 key 换命令 → `idempotency_conflict`。
4. `verify_scenario`（`start_pie:false`，`command:health` + `wait`）→
   `verified:true`，JUnit 写入项目 Saved。
5. `verify_scenario` 带 `delete_asset`（confirm）→ 步骤失败：
   `场景默认禁止高危命令 delete_asset`。
6. `animbp_probe` 真实 AnimBP → `class=AnimBlueprint`、is_animbp=true、
   `target_skeleton` 可读、`apis.get_animation_graphs=true`、writes_supported=false。
7. `animbp_read` → `graph_count=15`，`supported=true`；
   节点级返回 `nodes_available=false` + warning（5.8 Python 未暴露 UEdGraph.Nodes）。
8. 9,000,000 字节 Content-Length → HTTP 413（上限 8,388,608）。
9. stdio 代理（`UE5_BRIDGE_URL` 指向本机桥）→ initialize/tools/list 34 工具，
   `ue5_execute_command` 调 `session_lock_status` 正常返回。

## 3. UE 5.1 实录

1. `health`：`engine_version=5.1.1-23901901+++UE5+Release-5.1`。
2. 会话锁冲突/释放、幂等重放：与 5.8 同口径通过。
3. 场景 `command` 步骤：`verified:true`。
4. `animbp_probe` 普通 Blueprint → `is_animbp=false` + warning；
   `animbp_read` → `不是 AnimBlueprint ...` fail-loud。

## 4. 残余风险

- AnimBP 节点级图内容在 5.8 未暴露（`Nodes` protected）；`read` 诚实降级，
  不宣称节点可读。
- 会话锁是协作式；直接 `/build`、`/connect` 不经过锁层。
- 真机只覆盖 command 场景，未在 v6.5 轮次重跑完整 PIE 属性/截图场景；
  v6.2–v6.4 的 PIE 场景验证记录仍有效。
- 未做跨机网络/防火墙组合测试；HTTP 仅回环。
