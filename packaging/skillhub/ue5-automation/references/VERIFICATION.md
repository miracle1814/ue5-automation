# 验证记录摘要（v6.5.4）

本 Skill 对应的运行包版本：v6.5.4 / skill v1.14.0。

## 离线回归

```text
820 passed / 95 skipped / 0 failed
```

95 skipped 是需要真实编辑器的在线用例；不是失败。

## 真机

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
- 会话锁是协作式；
- 无 TLS；
- 本轮未重跑完整 PIE 属性/截图场景，沿用 v6.2–v6.4 记录。
