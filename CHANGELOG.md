# Changelog (中文精简版)

以下为主要变化摘要。

## [3.3] — 2026-09-22（skill v0.19）
- **MCP 工具目录规范化（面向 AI 客户端的工具选择质量）**：
  - 命名统一为 `ue5_<verb>_<noun>`：`ue5_health→ue5_check_health`、`ue5_diag→ue5_run_diagnostics`、
    `ue5_compile→ue5_compile_blueprint`、`ue5_logs→ue5_read_logs`、`ue5_batch→ue5_run_batch`、
    `ue5_command→ue5_execute_command`、PIE 6 个 → `ue5_start_pie` / `ue5_stop_pie` /
    `ue5_read_pie_state` / `ue5_list_pie_actors` / `ue5_read_pie_property` / `ue5_set_pie_transform`。
  - 23 个工具描述重写为「英文主 + 中文注」，含 Use-when 指引与返回形态；`ue5_execute_command`
    明确标注为"专用工具未覆盖时使用"的兜底入口。
  - 参数 schema 逐项补 description（原多数为空）。
  - 工具数仍为 23；内部 72 条命令白名单与 CLI 命令名不受影响。
- 离线回归：**503 通过 / 0 失败**（95 跳过 = 需活体编辑器的在线用例）。
- 已知遗留：UE 5.8 官方-MCP 工具集（`bridge/ue58-extras/`，8 工具）命名暂未同步，留待下版对齐；
  `manifest.json` 为打包期生成产物，MD5 清单待下次打包时再生成。

## [3.2] — 2026-09-18（skill v0.18）
- **UE 5.8 全链路打通**：C++ 桥按 5.8 编译（MSVC 14.44.35207）并通过官方 MCP 端到端验收
  （88 个桥方法、UMG 支持可见）。
- **预编译 5.8 桥随包**：`bridge/prebuilt-5.8/`（DLL + UnrealEditor.modules）→ 5.8 零编译。
- `build_for_engine.bat` 修复：自动同步 `UnrealEditor.modules`（缺失会被 UE 拒载）。
- 源码 5.8 兼容修复：`DoesPackageExist`/`SavePackage` 统一现代签名（5.1/5.8 单套代码）。
- `ue58-extras` 工具集加固：工具返回 JSON 字符串（`dict` 返回不被注册器支持）；
  可选字符串参数使用 `"*"` 哨兵默认（空串默认会被 schema 生成器视为必填）。
- 文档：BUILD.md / UE_VERSION_GUIDE.md 更新 5.8 工具链要求（MSVC ≥ 14.44 且 VS2022 ≥ 17.14）。
- **P0 崩溃修复（前置守卫 GameThread 分流）**：`/Game/` 内容类路径的
  `load_class`/`load_object` 探测改投 GameThread 执行——非 GameThread 触发的
  `LoadPackage` 会命中 `IsInGameThread()` 断言使编辑器直接退出（实测复现两次）；
  `/Script/` 引擎路径与离线测试维持调用线程直呼。全量回归 478 通过 / 0 失败。

## [3.1] — 2026-09-18（skill v0.17）
- **UMG / 材质写操作回读比对**：设置后强制读回校验，不一致即 fail-loud。
- 新增 C++ `ReadWidgetProperty`；共享 `_value_matches()` 值比对助手
  （浮点容差 / 结构体导出格式差 / 引号差异）。
- **FSlateColor 假成功修复**（回读机制首个实战战果）：
  `(R=..,G=..,B=..,A=..)` 会被静默接受但值不变 → 自动按
  `(SpecifiedColor=(...))` 重试并回读确认。

## [3.0] — 2026-09-18（skill v0.16）
- **MCP 协议接入层**：`/mcp` 端点（零依赖手写，兼容 UE5.1 内嵌 Python 3.9）。
- **PIE 运行时验证族**：`pie_start/stop/state` + 运行时对象枚举/属性读取/传送回读。
- **Actor 基础族**：`find_actors` / `spawn_actor`（可选 mobility）/
  `set_actor_transform` / `get_actor_properties`。
- **日志自取**：`logs_read`（游标增量 + 过滤）。
- **批量执行**：`editor_batch`（命令序列单次请求）。
- **C++ 桥源码随包** + `build_for_engine.bat` 一键重编译（5.2–5.8 自助）。

## [2.20] — 2026-09-18（skill v0.15）
- 独立验证轮：修复 13 项缺陷（E-1~E-13）——诊断引擎默认调用崩溃、
  Windows 图渲染崩溃、编译失败谎报成功、disconnect/delete 假成功、
  5.2–5.5 误部署拦截、冷启动 token 竞态等。
- 文档全库口径统一；泄漏扫描零命中。

## [2.19] — 2026-09-17（skill v0.14）
- 蓝图缺陷族 D-1~D-8：事件存在性误判（静默漏建）、同名节点 GUID 定位、
  `add_function_node` 回传 GUID、`delete_asset` 保存作用域收窄、
  `/build` events/functions 字段契约。
