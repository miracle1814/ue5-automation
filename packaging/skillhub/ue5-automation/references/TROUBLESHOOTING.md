# 常见故障排查（Top 10）

## 1. 403 / Unauthorized

- 读 `%APPDATA%\ue5_bridge\token`，客户端 `X-Skill-Token` 必须与 bridge 一致。
- **没有默认 `test123`**；token 文件不存在时，先启动 UE 让 bridge 生成。
- 用 `?token=` 只是兜底，优先用请求头。

## 2. `health` 连不上 / 端口占用

- 确认 UE 已启动、插件已加载。
- Output Log 搜 `[ue5_bridge] [OK] 服务已在后台启动`。
- 端口 8889 被另一个编辑器占用时，设 `UE5_BRIDGE_PORT=8890` 后重启该编辑器。

## 3. `apply_graph` 报“无法定位类”

函数节点必须带**完整类路径**：

```json
{"id":"print","kind":"function",
 "class":"/Script/Engine.KismetSystemLibrary",
 "target":"PrintString"}
```

只写 `KismetSystemLibrary` 在 5.8 上会定位失败。

## 4. 引脚默认值没生效

`pin_defaults` 是**图级字段**，不是节点字段：

```json
"pin_defaults":{"print.InString":"Hello"}
```

写在单个节点里会被忽略。

## 5. 同名节点改错 / 连线连错

- 同名节点不要只用标题定位；先用 `read_nodes` / `search_blueprint_nodes`
  拿 32 位 `node_guid`，再用 GUID 操作。
- 标题歧义时工具会 fail-loud 拒绝，不要硬猜。

## 6. `connect_pins` 返回成功但实际没连上

- 不要只看返回值；用 `read_connections` 复核拓扑。
- 引脚名必须精确；不确定时先 `search_blueprint_nodes` 看签名。

## 7. 编译失败

- 用 `compile_and_diagnose`，按返回的 `node_guid` / 图 / 节点标题定位。
- 不要用日志 tail 猜；tail 可能含上一轮历史错误。
- 修复后以 `compiled=true` 收敛，不要只看“无报错文本”。

## 8. PIE 断言失败 / 属性读不到

- `verify_behavior` 默认要求 PIE/Simulate 正在运行；先 `pie_start`。
- 5.1 用 `pie_mode:"simulate"`；5.8 可用 `"pie"`。
- Actor 名要匹配运行时世界里的名字；PIE 里 `GetActorLabel` 不可用。

## 9. 场景验收卡住

- 场景必须在 HTTP 线程单独调用，不要放进 `editor_batch`。
- `wait`/日志轮询受总超时限制（默认 300s）；拆小场景。
- 高危命令默认拒绝；确实需要时显式 `allow_destructive=true`。

## 10. 大图/大场景上下文爆炸

- 读蓝图用 `fields` 裁剪节点/连线字段。
- 只读 `read_blueprint` 的 L1，需要时再读 L2。
- 不要一次把整张图的所有节点塞进上下文；先按 GUID/标题定位。

## 仍然失败时

收集以下信息再排查：

1. `health` 返回（引擎版本、`bridge_loaded`）；
2. 失败命令的完整返回体（`error` / `category` / `verified`）；
3. 相关 `node_guid` 或资产路径；
4. Output Log 中 `[ue5_bridge]` / `BlueprintPythonBridge` 附近 50 行。
