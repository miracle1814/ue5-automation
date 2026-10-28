# v6.5 · 多 Agent 协作 + 稳定性合并（2026-10-28）

> 基线：v6.4.1 · 合并来源：`已优化版本A`（UETool v6.2.1-dev）
> 原则：只吸收增量；不覆盖 v6.3–v6.4.1 的回执/锁/场景/安全/面板实现。

## 新增能力

1. **幂等键**（`idempotency_key`）：网络/agent 重试不重复写；结果重放带
   `idempotent_replay: true`；跨命令复用 → `idempotency_conflict`。
2. **显式会话锁**：`session_lock_acquire/release/status`；与任务锁互相可见；
   全局 1024、单 owner 256、单次 200 资产、TTL 5–86400s（NaN/Inf 夹取）。
3. **回执查询**：`receipt_list` 返回最近 200 条写回执；回执新增 `receipt_id`、
   `idempotent_replay`，保留 before/after sha1。
4. **AnimBP 只读**：`animbp_probe` / `animbp_read`；读不到 fail-loud；
   写入通道未启用（需真机验证）。
5. **场景安全**：`command/pie_start/pie_stop` 步骤、高危命令默认拒绝、
   总超时、禁止递归/批量绕过；兼容 `type` 与 `action` 两种写法。
6. **HTTP 体积上限**：8MB（`UE5_MAX_REQUEST_BYTES`），非法 Content-Length → 400。
7. **stdio 去 requests**：标准库 urllib；保留离线/超时/403 三种 JSON-RPC 错误。
8. **token 去默认值**：缺失返回空串；桥启动后重读 token；403 自动刷新重试。
9. **工具面**：gateway 5→12，新增 `UE5_MCP_PROFILE=gateway|core|full`；
   命令 95→101，工具仍 34（新能力走 command 面，避免常驻上下文膨胀）。
10. **发布自检**：`tools/verify_release.py`、`tools/check_tool_profile.py`、
    `.github/workflows/ci.yml`、交叉验证/环境记录文档。

## 兼容性

- 旧调用不需要改：`session_id`/`idempotency_key` 都是可选参数；不传时行为不变。
- 旧的环境变量 `UE5_MCP_GATEWAY` / `UE5_MCP_CORE_TOOLS` 仍有效；
  `UE5_MCP_PROFILE` 优先。
- 场景旧 `type` 语法完全保留；`action` 只是新增等价写法。
- 回执新增字段，不删除旧字段。

## 验证

- 离线：`820 passed / 95 skipped / 0 failed`。
- 发布自检：`verify_release.py` 全过。
- 真机：UE 5.1 + 5.8（见 `CROSS-VALIDATION-v6.5.md`）。

## 未做/边界

- AnimBP 写入通道未开放；
- 显式锁是协作式，不是编辑器强制锁；绕过 `execute_command` 的直连端点
  （`/build`、`/connect`）不经过锁层；
- 无 TLS；跨机使用请自建隧道。
