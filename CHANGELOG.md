# Changelog (中文精简版)

以下为主要变化摘要。

## [3.3.1] — 2026-09-23（skill v0.20）
- **材质族写操作补回读验证（把蓝图侧的 P04 教训平移过来）**：
  蓝图侧早已证实「引擎 API 的 bool 返回值会说谎」（`TryCreateConnection` 是
  *可行性验证* API，类型不匹配时建隐式 Cast 节点仍返回 True → 现走四重验证）。
  材质侧 `connect_material_expressions` / `connect_material_property` 是**同类
  API**，原先只判返回值 → 同一类假阳性无防护。本次补齐：
  - `material_add_expression`：回读确认表达式真的出现在材质表达式清单中；
  - `material_connect_expressions`：回读目标输入引脚实际连到的表达式并比对，
    另检测「连线后新增表达式」= 引擎插入的隐式类型转换节点（P04 同类信号）；
  - `material_connect_property`：回读材质主属性（`base_color` 等）实际连到的表达式。
  降级口径对齐 `_pywrap_readback_nodes` 的 R1 策略：**能读到就严格比对，读不到
  只记 `warnings` 不阻断**（避免非 GameThread 等场景制造假阴性）。返回体新增
  `verified` 字段，使"验证没发生"成为可被 agent 检查的一等信号。
  ⚠️ **语义边界**：`verified` 与 `warnings` **解耦**——它只回答"回读比对是否
  执行且通过"，**不因非致命告警变 false**（例：检测到引擎插入的隐式转换节点时，
  连线本身已按引脚比对通过 → `verified=true` + 一条 warning）。比对读不到时
  才 `verified=false` + 告警。
- **`widget_add_child` 补回读**：用 `read_widget_tree` 确认控件真的进了树。
  同族 `widget_set_property` 早有 `ReadWidgetProperty` 回读，本条原先缺失 →
  族内验证密度不一致。
- **`material_compile` 修 `is False` 漏判**：原写法 `if ok is False` 会放过
  `None`（引擎 API 失败路径返回 None 是常态）→ 把未编译报成 `compiled:True`。
  改为与全项目一致的 `if not ok`，错误文本附实收值。
- **`compile_blueprint` 补 `success` 键（MCP `isError` 漏点修复）**：
  MCP 层判据是 `result.get("success") is False`，而该命令只报 `compiled:false`
  → **编译失败会以 `isError:false` 返回客户端**，agent 据 `isError` 判定时会当成功
  （ibrews 手册 §9.4「Treat isError as load-bearing」即此约定）。现补 `success`。
- **MCP 层 `is_err` 同时认 `success` 与 `ok`**：蓝图节点/变量/组件族共 36 条写命令
  的返回体只有 `ok`（`_pywrap_ctor_result` / `_pywrap_node_result`），原先只判
  `success` → 这些命令一旦改成「返回 `ok:False` 而非 raise」就会静默假成功。
  当前它们靠 raise 兜住，本次把这条隐式契约显式化。
- **测试基础设施：`fake_unreal.py` 补材质/控件族替身**（原先 `grep -ci material` = 0）
  → 材质族此前**零离线用例**，这是「验证密度低于蓝图族」的直接原因。新增：
  `FakeMaterial` / `FakeMaterialExpression` / `FakeExpressionInput` /
  `MaterialEditingLibrary` / `MaterialProperty` / `FakeWidgetBlueprint`，
  以及 8 个故障注入开关（`set_mat_connect_false_positive` 复现 P04 类假阳性、
  `set_mat_add_expression_drop`、`set_mat_insert_converter`、
  `set_mat_readback_unavailable` / `set_mat_verify_unavailable`、
  `set_mat_compile_result`、`set_mat_widget_add_drop` / `set_mat_widget_prop_drop`）。
  顺带修 `FakeClass.get_path_name()` 缺失（真实 UClass 有）。
- 新增 `tests/test_mat_readback.py`：**25 个用例**，覆盖正路径 / 假阳性 fail-loud /
  隐式转换告警 / 回读降级（连线与主属性两路）/ `verified` 语义边界回归 /
  `None` 漏判回归 / `success` 键回归 / MCP `isError` 契约。
- 离线回归：**504 通过 / 0 失败**（95 跳过 = 需活体编辑器的在线用例；基线 479）。

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
