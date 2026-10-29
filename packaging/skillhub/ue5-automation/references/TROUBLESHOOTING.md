# 常见故障排查

## 1. 403 / Unauthorized

- 读 `%APPDATA%\ue5_bridge\token`，客户端 `X-Skill-Token` 必须与 bridge 一致。
- **没有默认 token**；token 文件不存在时，先启动 UE 让 bridge 生成。
- 用 `?token=` 只是兜底，优先用请求头。
- 这不是云 API Key，不需要在任何模型平台配置。

## 2. `health` 连不上 / 端口占用

- 确认 UE 已启动、插件已加载。
- Output Log 搜 `[ue5_bridge] [OK] 服务已在后台启动`。
- 端口 8889 被另一个编辑器占用时，设 `UE5_BRIDGE_PORT=8890` 后重启该编辑器。

## 3. UE 5.8 装了插件但 8889 没监听

- UE 5.8 起不再执行项目自身 `Content/Python/init_unreal.py`。
- 确认 `Plugins/ue5-automation-extras/Content/Python/init_unreal.py` 存在。
- 插件侧 init 是 5.8 唯一自动入口；`ue5_bridge.start()` 幂等，重复调用无害。

## 4. `apply_graph` 报“无法定位类”

- 函数节点必须带**完整类路径**：
  `class:"/Script/Engine.KismetSystemLibrary"`。
- 只写 `KismetSystemLibrary` 在 5.8 上会定位失败。

## 5. 引脚默认值没生效

- `pin_defaults` 是**图级字段**，不是节点字段：
  `"pin_defaults":{"print.InString":"Hello"}`。
- 写在单个节点里会被忽略。

## 6. 同名节点改错 / 连线连错

- 同名节点不要只用标题定位；先用 `read_nodes` / `search_blueprint_nodes`
  拿 32 位 `node_guid`，再用 GUID 操作。
- 标题歧义时工具会 fail-loud 拒绝，不要硬猜。

## 7. `connect_pins` 返回成功但实际没连上

- 不要只看返回值；用 `read_connections` 复核拓扑。
- 引脚名必须精确；不确定时先 `search_blueprint_nodes` 看签名。
- 纯函数没有 exec 引脚；float/int 不能直接连 bool。

## 8. 编译失败

- 用 `compile_and_diagnose`，按返回的 `node_guid` / 图 / 节点标题定位。
- 不要用日志 tail 猜；tail 可能含上一轮历史错误。
- 修复后以 `compiled=true` 收敛，不要只看“无报错文本”。
- `errors=[]` 不等于没有错误：看 `warnings`、`structured`、`errors_scope`。

## 9. 编译结果为 Unknown

- Live Coding 等场景没有结果 API，Unknown 表示“不知道成没成”，不是失败。
- 不要为了让流程继续而写成 `compiled=true` 或 `compiled=false`。

## 10. PIE 断言失败 / 属性读不到

- `verify_behavior` 默认要求 PIE/Simulate 正在运行；先 `pie_start`。
- 5.1 用 `pie_mode:"simulate"`；5.8 可用 `"pie"`。
- Actor 名要匹配运行时世界里的名字；PIE 里 `GetActorLabel` 不可用。

## 11. 场景验收卡住

- 场景必须在 HTTP 线程单独调用，不要放进 `editor_batch`。
- `wait`/日志轮询受总超时限制（默认 300s）；拆小场景。
- 高危命令默认拒绝；确实需要时显式 `allow_destructive=true`。

## 12. 截图失败 / 截到旧画面

- 编辑器失焦或最小化会暂停帧推进；把编辑器调到前台重试。
- `camera_source=fallback` 说明没找到视口/玩家，构图不可信，先换机位。
- 同一场景改 pitch/yaw 连拍对照，图几乎相同说明看的是缓存/错位画面。
- PIE 首帧截图可能超过 2s，工具会放宽轮询；仍失败就重试一次。

## 13. 大图/大场景上下文爆炸

- 读蓝图用 `fields` 裁剪节点/连线字段。
- 只读 `read_blueprint` 的 L1，需要时再读 L2。
- 不要一次把整张图的所有节点塞进上下文；先按 GUID/标题定位。
- 写图用 `apply_graph`，不要逐节点搭。

## 14. 中文路径 / 多引擎编译失败

- 仓库与项目路径使用纯 ASCII；中文路径会触发 MSVC C1083（PCH 路径乱码）。
- 用户级 `BuildConfiguration.xml` 可能钉死 VS2019 工具链；编 5.8 时临时挪开
  三份用户配置（`%APPDATA%`、`Documents`、`%APPDATA%\Local`）再编译。
- UE 5.2–5.7 需要按源码重编译 bridge，不能直接用 5.1/5.8 DLL。

## 15. `unreal.*` 调用报 GameThread 错误

- UE API 只能在 GameThread 调用；bridge 已把命令队列切到 GT。
- 如果你自己写脚本直连，必须走 bridge 的命令通道，不要从 HTTP 线程直接调
  `unreal.*`。

## 16. 撤销不可用 / 撤销步数为 0

- `GetUndoCount()` 是“已撤销、可重做”的条数，不是可撤销数。
- 以 `agent_undo_status` 返回的 `agent_undoable` / `next_title` /
  `redo_count` 为准。
- 资产创建/删除不在撤销栈；用户穿插操作会让回滚提前停。

## 17. 工具数量比文档少

- 默认 gateway 档只有 11 个入口，这是有意的上下文治理。
- 用 `ue5_tool_search` 发现，或切 `core` / `full` 档后重启。
- 能力边界看命令白名单，不是看 `tools/list` 长度。

## 仍然失败时

收集以下信息再排查：

1. `health` 返回（引擎版本、`bridge_loaded`、`mcp_tool_profile`）；
2. 失败命令的完整返回体（`error` / `category` / `verified`）；
3. 相关 `node_guid` 或资产路径；
4. Output Log 中 `[ue5_bridge]` / `BlueprintPythonBridge` 附近 50 行。
