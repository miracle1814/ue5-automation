# UE5.1 自动化工具 — 使用手册

> **Skill 名称：** `ue5-automation`  
> **版本：** v0.18（T-20260918-V32-UE58 · 5.8 桥编译实证 + extras 端到端验收；v0.17 为回读补强）
> **历史版本记录：** v0.17（T-20260918-V301-READBACK · 回读置信度补强：UMG/材质设值回读比对 + FSlateColor 自适应；v0.16 为融合升级）
> **历史版本记录：** v0.16（T-20260918-V3-FUSION · 融合升级：MCP 协议接入 + PIE 验证族 + 材质/UMG 新域 + 源码随包；v0.15 为独立验证轮）
> **历史版本记录：** v0.15（T-20260918-VERIFY-FIX · 独立验证轮：缺陷族 E-1~E-10 修复 + 全库口径清理，见下方 v0.15 日志；v0.14 历史内容保留其后）
> **历史版本记录：** v0.14（T-20260917-SKILL-CLEANUP-V219 · 蓝图缺陷 **D-5~D-8** 修复 + 文档/元数据口径清理：`/build` 的 `events[]` / `functions[]` **字段名契约对齐**（照文档写不再得空名）· `add_function_node` **回传 node GUID** · `delete_asset` **保存作用域收窄**（不再静默提交未保存关卡））  
> **手册版本：** v2.3  
> **更新日期：** 2026-09-18（v0.16）  
> **适用 UE 版本：** **5.1.x 开箱即用**；5.2–5.5 需自行重编译 bridge；5.6+ 未验证 —— **升级路径与出口见 [`UE_VERSION_GUIDE.md`](UE_VERSION_GUIDE.md)**（随包）
>
> **📦 随包范围（第三方使用者必读）：** 发布包只含**运行时脚本**（`scripts/` / `templates/` / 文档）。`tests/`、`conftest.py`、`run_tests.bat` 属**开发端测试基建**，**默认不随发布包**（仅 `publish.py --with-tests` 时携带）——下文历史更新日志中出现的 `run_tests.bat` 命令均为**开发端**用法，第三方发布包内**无此文件**，请勿尝试执行。
>
> ### v0.18 更新日志（T-20260918-V32-UE58 · 5.8 桥编译实证 + extras 端到端验收）
> - ✅ **UE 5.8 侧全部打通（实测）**：C++ 桥按 5.8 重编译成功（引擎 **5.8.2-56702186** · 工具链 **MSVC 14.44.35207**）并完成端到端验收 ——
>   `5.8 编辑器 → 官方 MCP(127.0.0.1:8000) → UE5AutomationExtras 工具集 → C++ 桥（**88 个方法**，含全部 UMG API）`：
>   `ue5_status` 回读 bridge_loaded=true / method_count=88 / has_umg=true；`ue5_pie_state`、`ue5_actor_find`（无参枚举 50 actor）、`ue5_logs` 实调通过；错误路径 fail-loud 可读。
> - 📦 **预编译 5.8 DLL 随包**：`bridge/prebuilt-5.8/`（DLL + **UnrealEditor.modules**）→ 5.8 用户**零编译**即可获得节点级桥能力（复制两文件到 `<Project>/Plugins/BlueprintPythonBridge/Binaries/Win64/`）。
> - 🔧 **5.8 兼容修复（源码级）**：`FPackageName::DoesPackageExist` 的 Guid 重载在 5.8 移除 + `UPackage::SavePackage` 老参列重载移除 → 统一改现代签名（5.1/5.8 同一套代码）→ 单源码跨版本。
> - 🩹 **`build_for_engine.bat` 修复**：现在**同时同步 `UnrealEditor.modules`**（UE 模块清单含 BuildId，缺失会以 `Incompatible or missing module` 拒载——5.8 冒烟实测踩中并修复）。
> - 🧩 **extras 工具集两处实修（5.8 实测）**：① 工具方法返回 `dict` 会被注册器拒绝（`missing specification for contained type`）→ 统一 `-> str` + JSON 字符串；② 空字符串默认值会被 schema 生成器视为**必填** → 可选字符串参数改 `"*"` 哨兵默认（空串/星号均表示"不过滤"）。
> - 📘 **5.8 官方 MCP 开启方式（实测必需）**：MCP HTTP 服务**默认不自动启动**——项目 `Config/DefaultEditorPerProjectUserSettings.ini` 写：
>   `[/Script/ModelContextProtocolEngine.ModelContextProtocolSettings]` + `ServerUrlPath=/mcp` + `ServerPortNumber=8000` + `bAutoStartServer=True`，重启编辑器后监听 127.0.0.1:8000。
> - ⚠️ **工具链要求（实测排定）**：5.8 编译需 **MSVC ≥14.44**（14.40–14.43 被 UBT 封禁；14.38 撞引擎 C7539）且 VS2022 产品本体需 ≥17.14。

> ### v0.17 更新日志（T-20260918-V301-READBACK · 回读置信度补强 + 5.8 工具链结论）
> - 🎯 **取舍#6 落地：写操作回读比对覆盖补满**（"绝不假成功"的最后一环）：
>   - **UMG 设属性**：C++ 新增 `ReadWidgetProperty`（导出属性文本；失败透传 `ERROR:` 前缀）→ `widget_set_property` 设置后**强制回读比对**，不一致即 fail-loud（返回体新增 `readback`/`verified`/`applied_value`）。
>   - **材质设属性**：Python 侧回读比对（此前只回传不回判）→ 不一致即 fail-loud（新增 `verified: true`）。
>   - 新增共享助手 `_value_matches()`：精确 → 数值容差（f-string 浮点显示差）→ 去引号 → **数字 token 序列比对**（结构体导出格式差）→ 覆盖三类真实差异。
> - 🔴 **回读机制首战抓到真实假成功（并已修复）**：`ColorAndOpacity` 是 **FSlateColor**，用美术习惯的 `(R=..,G=..,B=..,A=..)` 格式会被 ImportText **静默接受但值不变**（回读恒为白色）——v3.0 报告中该用例的"成功"实为**假阳性**，特此更正。修法 = **自适应格式重试**：回读发现回读文本含 `SpecifiedColor=` 且请求未包装时，自动以 `(SpecifiedColor=(...))` 重试并回读确认（返回体 `note` 透明记录）。实测：请求 (0.1,0.2,0.3,1.0) → 应用包装格式 → 回读 (0.100000,0.200000,0.300000,1.000000) → verified=true。
> - 🔧 **5.8 桥编译工具链结论（实测排定）**：UE 5.8 要求 **MSVC ≥ 14.44**（`static_assert(_MSC_VER >= 1938)` 是下限声明，但 **14.38 实测撞引擎 `ContainerAllocationPolicies.h` C7539 严格一致性错误**；14.40–14.43 被 5.8 UBT 明令封禁）→ 可行区间 = **14.44+（建议 14.50.35717）**。安装：VS Installer `modify --add Microsoft.VisualStudio.Component.VC.14.44.17.14.x86.x64`（需 UAC 提升）。
> - 📊 回归：**502 passed / 95 skipped / 0 failed**（+5 为 `_value_matches` 单测）。
> - 包内 DLL 更新：**901,120 B · MD5 0434743859C997C5711519FCDD449F97**（+UMG 回读 API；5.1 已活体验证）。

> ### v0.16 更新日志（T-20260918-V3-FUSION · 融合升级：MCP 协议接入 + 编辑器广度能力 + 材质/UMG 新域）
> - 🚀 **MCP 协议接入层（`/mcp` 端点 · 零依赖手写）**：官方 mcp SDK 需 Python ≥3.10，而 UE5.1 内嵌 Python 为 3.9 → 手写最小 MCP（JSON-RPC 2.0 / Streamable HTTP：initialize 版本协商 · tools/list · tools/call · ping · notifications 202 · resources/prompts 空列表）。**23 个 MCP 工具**（诊断/读写/构建/PIE/日志/批量 + `ue5_command` 白名单透传，描述含全量命令名）；鉴权新增 `Authorization: Bearer` 与 `?token=` 双兼容通道（原 X-Skill-Token 不变）。任何 MCP 客户端（ZCode/Claude 等）可直连 **UE 5.1–5.5** 编辑器 —— 接入文档见 `docs/MCP-ACCESS.md`。
> - 🎯 **编辑器广度能力（移植自官方 MCP 验证范式）**：
>   - **PIE 运行时验证族**：`pie_start(mode=simulate|pie)` · `pie_stop` · `pie_state` —— 跨库候选链实测修正（UE5.1：`editor_play_simulate` 在 EditorLevelLibrary，`editor_request_end_play`/`is_in_play_in_editor` 在 LevelEditorSubsystem，`editor_end_play` 才是 ELL 的停止名——旧单库探测全 missing）；**实测：PIE 期间 HTTP bridge 存活**（运行时读属性/传送全通）。
>   - **Actor 基础族**：`find_actors` · `spawn_actor`（新增 `mobility` 参数）· `set_actor_transform` · `get_actor_properties`（world=editor|pie）。
>   - **日志查询**：`logs_read`（游标增量 + category/pattern 过滤）—— 诊断模式从"用户贴日志"升级为"Agent 自取日志"。
>   - **批量执行**：`editor_batch`（白名单命令序列单次请求，fail-loud 可中止）。
> - 🔴 **实测修复：PIE 传送假成功**（"返回值说谎"族）：`set_actor_location(vec, False, False)` 在 PIE 中对 Static 组件**静默无效**但命令报 success=true → 改为 teleport=True 优先 + **设置后回读比对，未生效即 fail-loud**（并回传 mobility 供排查）。实测：Movable Actor 传送 verified=true + 独立复读一致。
> - 🎨 **材质族（9 条命令 · 1 个新 C++ API）**：探针实测 UE5.1 `MaterialEditingLibrary` 完整暴露（72 方法）→ `material_create` / `material_add_expression` / `material_connect_expressions` / `material_connect_property`（base_color 等 14 种属性映射）/ `material_set_expression_property`（类型自适应 + 回读回传）/ `material_compile` / `material_read` / `material_instance_create` / `material_instance_set_parameter`（scalar/vector/texture）。
> - 🧩 **UMG 控件蓝图（4 条命令 + 4 个新 C++ API）**：`UWidgetTree` 未向 Python 暴露（与 UEdGraph 同坑）→ C++ 桥新增 `CreateWidgetBlueprint`（父类 UUserWidget + 自动建树编译）/ `AddWidgetToTree`（根或面板子级，重名 fail-loud）/ `SetWidgetProperty`（FindFProperty + ImportText_Direct）/ `ReadWidgetTree`；命令层 `widget_create` / `widget_add_child` / `widget_set_property` / `widget_read_tree`（旧 DLL 上调用 → 可读缺件提示 + 重编译指引，绝不假成功）。WidgetBlueprint 的 EventGraph 复用既有节点级图能力。
> - 📦 **白名单 50 → 72 条**（+PIE×3 + Actor×4 + logs + batch + 材质×9 + UMG×4）；`/command` docstring 同步。
> - 🔧 **C++ 桥源码随包**：`bridge/BlueprintPythonBridge/Source/`（.cpp/.h/Build.cs 全量）+ `build_for_engine.bat <UE_ROOT>` 一键编译（自动备份旧 DLL + 部署）+ `BUILD.md`（5.2–5.8 自助重编译路径；含 Win32Exception 998 / VS 工具链 / API 变动三类问题的处理）→ UE_VERSION_GUIDE 路径 B 对第三方**真正可自助**。
> - 🧩 **5.8 官方 MCP 融合插件（`bridge/ue58-extras/`）**：纯 Content 插件（照 ZCodeMCPExtras 已验证模式）注册 `UE5AutomationExtras` 工具集 —— 8 个工具（status/PIE 状态/日志/Actor 查改/编译 + 桥部署后的**节点级图回读与带拓扑验证的连线**，即官方 DSL "exec 线为空"缺陷的正解路径）。安装 3 步见其 README。
> - 📊 **回归与实测**：离线套件 492 passed / 95 skipped / 0 failed（+38 v3.0 新用例：MCP 协议/鉴权/batch/logs/别名）；UE 5.1.1 活体：MCP 握手与工具调用、PIE 启动→状态→运行时对象→传送回读→停止、日志、批量全链路通过。
> - ⚠️ **诚实边界**：UE 5.8 的 C++ 桥重编译被本机 MSVC 工具链限制阻断（UE 5.8 UBT 封禁 MSVC 14.40–14.43，本机为 14.42）→ 5.8 侧交付源码 + 一键编译脚本 + 纯 Python 工具集；安装 14.50+ 工具链后运行 `build_for_engine.bat` 即可补齐节点级能力。

> ### v0.15 更新日志（T-20260918-VERIFY-FIX · 独立验证与修复轮）
> - 🔍 **本轮性质**：由独立验证方对 v2.19 发布包做全量审查（4 路静态审查 + 离线回归 + UE 5.1.1 活体实测），修复验证中发现的缺陷族 **E-1~E-10**（E = Evidence-based，区别于开发端自报的 D 族）。
> - 🔴 **E-1（诊断引擎默认调用即崩）**（`log_parser.py`）：日志含**未匹配错误行**（真实 UE 日志最常见形态）时 drafts 落盘对绝对路径调用"只许相对路径"的 sanitizer → 未捕获 ValueError → 主报告已写、drafts 全丢、进程崩溃。修法 = sanitizer 增加 `allow_absolute`（相对路径的 cwd 包含校验与 `..`/`~` 拒绝语义不变）；同时 `--output` 支持绝对路径、缺文件退出码 0→2、无操作 auto-fix 桩不再谎报 "applied"。
> - 🔴 **E-2（Windows 上 L1 图渲染必崩且不降级）**（`mermaid_render.py`）：npm 的 mmdc 是 .cmd 垫片，`subprocess` 裸名只按 .exe 解析 → FileNotFoundError 且异常类型不在捕获链 → 整批渲染崩溃。修法 = `shutil.which` 解析可执行文件 + OSError → MermaidRenderError（L2/L3 降级链恢复生效）；输出文件名加短随机后缀防同秒覆盖。
> - 🔴 **E-3（/build 编译失败被谎报成功）**（`game_thread_build.py.tmpl`）：`compile_blueprint` 的 bool 返回值被忽略、无条件记 step ok → 编译失败时 `/build` 返回 success=true + compile_status=BS_UpToDate（正是本项目自订"返回值说谎"红线）。修法 = 按返回值定论，False → step error + success=false。
> - 🔴 **E-4（disconnect/delete 假成功与不可达）**（`blueprint_editor.py`）：① `_get_pin_links` 只按标题匹配 + 读取失败也返回空 → GUID 入参/桥故障时谎报"本就无连接"且不执行断开（修法 = GUID 匹配 + 读取失败直调 /disconnect 权威定论；bridge 连线回传补 from_guid/to_guid）；② `_get_node_info` 硬编码 EventGraph → 函数图/宏/构造脚本节点永远"不存在"（修法 = 全图搜索）+ 读取失败与节点不存在可区分；③ 回滚承诺无实现（修法 = `rollback()` 实装：connect/disconnect/update_param 可逆，delete 明示不可逆）。
> - 🟠 **E-5（analyze_live 直连路径瘫痪）**（`analyzer.py`）：BlueprintReader 构造签名错 + 不存在的 to_dict + self.result 未初始化三重坏 → UE 直连扫描完全不可用。修法 = 对齐 BlueprintReader 实际 API + 惰性初始化；循环依赖去重补旋转规范化（同一环不再重复上报）。
> - 🟠 **E-6（5.2–5.5 引擎误部署 5.1 DLL 零拦截）**（`bridge/deploy.py` + `install_dependencies.bat`）：新增**引擎版本闸**（读 Build.version / .uproject EngineAssociation，非 5.1.x 默认拒绝 + `--force` 显式覆盖）、PermissionError 可读化、--target 校验 .uproject、HKCU 注册表探测；bat 补部署失败 errorlevel 检查（不再假成功）、管理员运行兼容、Python ≥3.10 版本闸、CRLF 规范化。
> - 🟠 **E-7（读通道引擎发现退化）**（`ue_send.py`）：移除开发机硬编码路径 → 注册表（HKCU+HKLM / WOW64_64KEY）+ 各盘符 × UE 5.1~5.5 动态探测；`send(timeout)` 形参真正生效（发现阶段）；`--bridge-diag` 解析结构化结果并按 bridge_loaded 设退出码（0/4）。
> - 🟡 **E-8~E-10（健壮性）**：`check_ue_version.py` 本机探测禁用代理（系统代理下 127.0.0.1 误报）；`preflight.py` 批量 manifest 数组形态 + 颜色 isatty 探测；`blueprint_builder.py` CLI spec 嵌套转换 + 失败退出码；`blueprint_reader.py` save_report 两处崩溃/覆盖；`blueprint_editor.py`/`analyzer.py` GBK 控制台 emoji 崩溃；`setup.py` 死代码 DLL 路径 + 8889 端口冲突。
> - 📚 **文档口径清理**：token「默认 test123」错误口径三处更正（bridge 端随机 token + token 文件，无 test123）；/health 需鉴权（浏览器裸访问 403 非故障）；幽灵端点 /execute 两处更正为 /command；SKILL.md 白名单口径 48→50；UE「5.1 或更高」回潮表述按 UE_VERSION_GUIDE 收敛；手册版本统一 v2.0；各文档版本号对齐 v0.15。
> - 📊 **回归**：离线基线 `435 passed / 92 skipped / 0 failed` 保持全绿（1 个固化了过时数字的测试断言同步修正为动态口径）；UE 5.1.1 活体实测见随包《v2.20 验证报告》。
> - 🎯 本次修改范围：`scripts\` 11 文件 + `bridge\deploy.py` + 两个 install bat + `setup.py` + 全库文档；**⛔ 不改 bridge DLL、⛔ 不改 C++ 源码**。

> ### v0.14 更新日志（T-20260917-SKILL-CLEANUP-V219 · 蓝图缺陷 D-5~D-8 修复 + 文档/元数据口径清理）
> - 🎯 **D-5（`/build` 的 `events[]` 字段名契约与实现不一致 · 照文档写会得「空事件名」）**（`scripts/ue5_bridge.py` + `scripts/game_thread_build.py.tmpl`）：`BuildRequest.events` 注释与 `ue5_runner.build()` docstring 均写 `{"name": …}`，但 bridge 原样透传、模板只认 `event_name` → 传 `name` 得**空事件名** → step `add_event:` + `add_event_node returned None` + `compile_status=BS_Error`（**四条报错提示全指错方向**）。
>   - **修复**：新增 `_normalize_build_events()`（**L5353**）+ `/build` 接线（**L5595**）——主字段 `event_name`、兼容别名 `name`；冲突取主字段并回传 `warnings`；两者皆空 → **fail-loud**；`game_thread_build.py.tmpl` 同步**双层兼容**。
> - 🔊 **D-6（`add_function_node` 不回传节点 GUID）**：返回体补回 **`node_guid`**（`_pywrap_recover_node_guid()` · `ue5_bridge.py` **L1733**）→ 建完节点**可直接**喂 `set_pin_default_value`（⛔ 不必再 `read_nodes` 回读全图按位置反查）。
> - 🩹 **D-7（`delete_asset` 保存作用域过宽 → 静默提交未保存的关卡 / 资产）**：旧实现第②步调 `save_dirty_packages(True, True)` = 保存**全工程脏包** → 用户「删一个临时资产」会连带写 `ElevatorDemo.umap` 等无关资产；改法 = **仅保存「引用被删资产的包」**（`_referencers_options()` **L4200** / `_collect_related()` **L4221** / `_save_related()` **L4256**；旧 `_save_dirty()` 整体删除），返回体新增 **`save_scope`** / **`saved_related_packages`** / **`save_skipped_reason`** 三个可观测字段。
> - 🎯 **D-8（`/build` 的 `functions[]` 字段名契约与实现不一致 · 与 D-5 完全同型）**：新增 `_normalize_build_functions()`（**L5425**）→ `/build` 接线 **L5603**、`/build-batch` 接线 **L7345**（两处 GT 透传 L5710 / L7414 均为**已归一化**结果）——主字段 `function_name` + 兼容别名 `name`、`function_path` **可由 `target_class` 推导**（`<target_class>:<函数名>`，并标注来源）、冲突取主字段 + `warnings`、**函数名与路径皆缺 → fail-loud**（点名 `functions[i]` + 回传**实收 keys**，且**早于任何副作用**：不建资产、不留半成品）；模板侧双层兼容（`game_thread_build.py.tmpl` L309-343）。
> - 📚 **文档 / 元数据口径清理**：
>   - `/command` 白名单口径 **48 → 50 条**（补登 `delete_asset` / `unpin_console_refs` 两行 —— 二者此前**从未登记进任何命令表**）→ `workflow.md` 附录 B / `API_REFERENCE.md` / `docs\L2-COMMANDS.md` / `docs\SOP-UE-HTTP-BRIDGE.md` **四处同步**。
>   - `config\error_patterns.json` 错误模式 **15 → 19 条**（PTN-016~019 登记闭环；其中 P16 原为**空头登记**「关联 PTN-016 但库内无此条」，一并补齐）。
>   - `docs\pitfalls.md` 新增 **P19**（`/build` spec 字段名契约不一致）+ 附录映射行 + 版本 v1.3；全库「陷阱 / 模式」计数口径同步（**18 → 19 个已知坑（P01–P19）**）。
> - 📦 **发布包口径修正**：`scripts\publish.py` 桥接段清单黑名单口径与**打包剔除侧**对齐（manifest `bridge.files` 补回**故意随包**的 `DELIVERY.md`，消除「包里有、清单里没有」的账实不符）；`setup.py` / `install_dependencies.bat` **建立真源血统**（原为「保留包内现有」的无源孤儿产物 → 现随真源同步）。
> - 📊 **回归**：离线基线 `405 passed / 92 skipped` → **`435 passed / 92 skipped / 0 failed`**（+30 全为 D-8 新增用例；`skipped` 与基线**逐数一致**）。
> - 🎯 本次修改范围：`scripts\ue5_bridge.py`（363,739 → **372,793 B**）+ `scripts\game_thread_build.py.tmpl`（22,584 → **24,984 B**）+ `scripts\ue5_runner.py`（29,433 → **30,119 B**）+ `docs\SOP-PIPELINE.md`（16,245 → **17,292 B**）+ `scripts\README.md`（6,183 → **6,331 B**）+ `docs\pitfalls.md` + `workflow.md` + `API_REFERENCE.md` + `docs\L2-COMMANDS.md` + `docs\SOP-UE-HTTP-BRIDGE.md` + `SKILL.md` + `USER_GUIDE.md` + `config\error_patterns.json` + `scripts\publish.py`；**⛔ 不改 bridge DLL、⛔ 不改 C++ 源码**。
> - ⚠️ **诚实边界**：本节所列**离线回归证据**来自开发端全量套件；**活体 UE 复测**（D-5/D-8 别名与 fail-loud · D-6 GUID 回传 · D-1/D-2 回归）由发布方独立执行，结论见随包交付报告。

> ### v0.13 更新日志（T-20260916-UE5AUTO-HALLBEACON-D1D4-FIX / T-20260917-RELEASE-V218 · 蓝图缺陷 D-1~D-4 修复随包）
> - 🔴 **D-1（阻断级 · `/build` 事件存在性判定误判 → 静默漏建）**（`scripts/game_thread_build.py.tmpl`）：`/build` 建非自建事件（如 `ReceiveActorEndOverlap`）时返回 `success=true`、**节点却没建**；根因 = 事件候选标题表把**所有**事件的本地化标题无条件塞进候选，与目标事件**无语义绑定** → 图中脚手架自带的「事件Actor开始重叠」误命中 → 判 `exists` → 静默跳过创建。
>   - **修复**：新增 `_norm_event_key()` 语义键（去 `event`/`receive` 前缀 + 去 `_`/空格 + 小写），**非本事件标题绝不入候选**；本地化映射表 9 → 15 键（补 `ActorEndOverlap` / `ReceiveTick` / `ReceiveDestroyed` 等缺口）；`exists` 步回传 `matched_by` / `matched_title` / `candidates`（**可审计**，不再"黑箱 exists"）。
>   - **效果**：不再静默漏建、不再重复创建同名事件；映射表外的事件名退化为「候选=[原名] → 建不出即 fail-loud」。
> - 🎯 **D-2（`set_pin_default_value` / `get_pin_default_value` 支持 GUID 定位）**（`scripts/ue5_bridge.py`）：`node_id` 现**同时接受节点标题与 32 位 GUID**（亦可用显式别名 `node_guid`）→ **同名节点场景可按 GUID 精确设值**（此前 3 个同名「设置Actor在游戏中隐藏」只能命中其一）；返回体新增 `matched_by` ∈ {`guid`,`title`}；**标题定位向后兼容**；**零 DLL 改动**（复用既有 pywrap `GetNodeByGuid` 通路）。
> - 🔊 **D-3（`add_function_node` 建节点失败 fail-loud）**：DLL 返回 None 时**必抛 `BridgeError`** 并附可读原因 + **函数名候选**（原名 / 去 `K2_` / 加 `K2_`），不再返回 `{"added_node": …}` 的**假阳性成功**（实测：`/Script/Engine.Actor:K2_SetActorHiddenInGame` → 真实函数名为 `SetActorHiddenInGame`，`K2_` 前缀仅用于重载冲突函数）。
> - 🩹 **D-4（`unpin_console_refs` 缺参裸 TypeError 修复 + 文档补充）**：白名单绑定改为默认 `""`，空参 / 无参返回**可读错误**（`success=false` + `error`），⛔ **不执行任何删除**（本命令**无全量清理语义**）；`API_REFERENCE.md` 同步补入参说明。
> - 📊 **回归**：离线基线 `344 passed / 92 skipped` → **`361 passed / 92 skipped / 0 failed`**（+17 全为本单新增用例，零跳过变化）。
> - 🎯 本次修改范围：`game_thread_build.py.tmpl`（16,739 → 20,766 B）+ `ue5_bridge.py`（337,977 → 346,945 B）+ `API_REFERENCE.md`（12,508 → 14,539 B）+ **本次文档同步**（SKILL.md / USER_GUIDE.md / workflow.md / `docs/pitfalls.md` / `docs/SOP-UE-HTTP-BRIDGE.md`）；**不改 bridge DLL、不改 C++ 源码、不改运行语义之外的行为**。
> - ⚠️ **诚实边界**：D-1 / D-2 的**在线（活体 UE）复验尚未执行**——本版仅含**离线回归**证据；D-1 最坏表现为「建不出 → fail-loud」，**不再静默漏建**。
> ### v0.12 更新日志（T-20260915-UE5PKG-VERSION-DOC · UE 版本适配与升级出口）
> - 📘 **新增 `UE_VERSION_GUIDE.md`（随包 · 根级）**：把"随包 DLL 只实测 5.1"从**一句免责声明**升级为**可执行的升级路径 + 出口**——
>   - **三层适配结构**（L1 C++ bridge DLL 强绑定引擎 / L2 UE 内 Python 中层 / L3 宿主侧 Python 不绑定），明确"换版本只需重编译一个 ~148 KB 模块"；
>   - **路径 A/B/C**：5.1 零改动 · 5.2–5.5 取源码重编译（含 `Build.cs` 模块依赖清单、VS2022 14.42 并行编译坑 `-NoParallelExecutor`）· 5.6+ 按编译错误分类排查；
>   - **§8 出口与降级矩阵**：拿不到源码时**仍可用**的只读能力（L1 原生探针 / 项目评估 / 报错诊断 / 部署自检为纯 Python），写通道不可用一栏如实标注；
>   - **§6 升级后验收 6 项**（含关键判据：`degrade_level` 必须为 `L2_BRIDGE`，否则 bridge 实际未生效）；
>   - 附 5.1 基线数字（`method_count=80` / `missing_apis=3`，实测自 `/diag`）与事实来源表。
> - 🧰 **新增 `scripts/check_ue_version.py`（随包 · 只读）**：注册表 + 常见目录 + `--engine` 定位引擎 → 读 `Engine\Build\Build.version` → 判定并给下一步；`--online` 追加 `/health` + `/diag` 探测；退出码 `0=5.1 可用 / 2=需重编译 / 3=未知或未检测到`，可直接用于脚本编排。
> - ✏️ **文档口径修正**：SKILL.md / `USER_GUIDE.md` 原先的"适用 UE **5.1+**"表述**作废**（DLL 仅实测 5.1，`5.1+` 会造成"高版本也能直接用"的误导），统一改为「5.1.x 开箱即用；5.2–5.5 需重编译；5.6+ 未验证」并指向本指南。
> - 🎯 本次修改范围：新增 2 文件（指南 + 自检脚本）+ `publish.py` 根白名单登记 + SKILL.md / USER_GUIDE.md / DEPLOY.md 三处口径与指针同步；**不改任何运行时代码逻辑**。
>
> ### v0.11 更新日志（T-20260909-SKILL-GEN-C-INIT-CONVERGE-REMAIN · 语义收敛定稿）
> - 🔴 **init.py 语义收敛（v1.0「编排器」→ v1.1「薄执行器」）**：v0.10 日志中「一键初始化/宿主适配**编排器**」表述**作废**。实际实现为**薄执行器**——只做探测 + 生成 role_assignments.yaml **建议稿（status: proposed）**，**不自动生效**
>   - **自动语义分配 = 预加载建议稿**（role_source: auto），未经人工确认 `status: proposed`，**不作为生效配置**
>   - **人工确认门**：`--assign <id>=<role>` 显式指派核心角色，或手编 role_assignments.yaml 后 `--reconcile --apply`，将角色升级为 manual → `status: confirmed` 才生效
>   - **single_agent 确定性指派**：≤1 enabled agent 时无选择空间 → 宿主 `all_in_one`（role_source: deterministic）→ `status: confirmed`（无需人工通道）
>   - **防静默覆盖 GATE**：既有 confirmed 配置 + 无人工确认通道 + role_map 变化 → 拒绝（exit 3），禁止自动建议稿覆盖生效配置
> - 🧪 **发布门禁 fixture**：新增 `tests/test_init_single_agent.py` 4 用例（single_agent 确定性指派路径：detect mode → all_in_one deterministic → decide confirmed 全链实测通过，0.19s）
> - 📋 **§2.5 部署清单语义同步**：改为「薄执行器 + 人工确认门」引导（见下）；DEPLOY.md「新机器初始化」章节 / API_REFERENCE.md init.py CLI/产物契约条目本次一并补全（兑现 v0.10 日志声明）
> - 🎯 本次修改范围：仅 SKILL.md / DEPLOY.md / API_REFERENCE.md 三文档 + tests fixture；不重写 init.py（62,601B / 1504 行已 py_compile 通过）
>
> ### v0.10 更新日志（T-20260909-SKILL-GEN-C-INIT · 通用化初始化入口）
> - 🚀 **新增 `scripts/init.py` 一键初始化/宿主适配编排器（v1.0，随包分发）**：phase0-5（参数解析 → 能力探测三级链 → UE 环境探测 → 一键配置 → health 自检 → 产物），解决 skill 跨机器/跨宿主机首次部署时的手工适配问题
>   - **能力探测三级链（phase1）**：L1 `list_agents API`（localhost:8088 `/api/agents`）→ L2 逐 agent 读 `agent.json`（tools 硬能力）→ L3 读 `system_prompt_files`（AGENTS/SOUL/PROFILE 语义标签）
>   - **UE 能力判定（4 硬项 + 软判据）**：enabled + shell + file_io + python 全过，且身份文本（description/name）命中 UE 词表 → `capable(high)`；system_prompt 命中仅增强不翻转（防团队 prompt 全判 capable 误判）
>   - **角色分配（role_assignments.yaml schema v1.0）**：orchestrator / ue_executor(+backup 两级) / screenshot_ocr / report_writer / archivist；单 Agent 环境自动 all_in_one；`--assign <id>=<role>` 手动覆盖（不抢自动 backup）、`--reconcile` 读回手编角色合并
>   - **默认 dry-run 只读**，`--apply` 才落盘 `<skill>/output/`（publish 黑名单，防漂移入包）；不删除既有文件；不创建任何自启/定时/常驻服务
>   - 离线自测：py_compile + 真实 11 agents dry-run（探测链/deploy 定位/UE 引擎 `（本机开发路径）` 全命中）+ 4 场景（默认/手动覆盖/backup/非法角色）+ apply 回读 + reconcile 手编保留
> - **📋 §2.5 部署清单重构**：改为 init.py 引导式（先探测 → 再按缺项部署），附人工兜底步骤
> - **📄 DEPLOY.md** 新增「新机器初始化」章节（二·五）；API_REFERENCE.md 新增 init.py CLI/产物契约条目

> ### v0.9 更新日志（T-20260908-CPPAPIS-CLOSEOUT · CPP-APIS 在线闭环收尾）
> - ✅ **T-20260823-UE5BRIDGE-CPP-APIS 在线闭环（2026-09-08 实测）**：SetComponentProperty + ReadVariables C++ API（8/23 离线 17 项通过）经 DLL 部署（437,248B · 旧版备份 .bak5）后在真实 UE（dev-project / 5.1.1 · Bridge method_count 48）验证通过
>   - **CPP-APIS 在线实测证据**：`read_variables` C++ 路径返回真实变量清单（BP_HelloMirror DefaultSceneRoot + 临时蓝图 **Health/MaxHealth**，消除 Python new_variables 假阳性/降级）；`add_component` properties → **SetComponentProperty** 设置 BoxExtent=[200,100,50] → spawn 实测 `CPP_Box Extent = {x:200, y:100, z:50}` **精确生效**（未 fail-loud）
>   - **UE 在线功能测试**：离线基线 133 passed · 5 skipped · 0 failed 零回归（53.33s）；e2e 38 passed 全绿（含真实 UE 在线 7 项 = blueprint_e2e 4 + analyzer_phase2 3）；mermaid `--ue-online` 9 passed（l2_online 解锁）→ 原 5 skipped 全解锁
>   - 验证固化：`docs/VERIFICATION-PHASE2.md` §七（2026-09-08 UE 在线功能测试）
> - **🔑 token 403 根因修复（2026-09-08）**：offline 测试模块 import 时 `setenv UE5_BRIDGE_TOKEN` 污染 pytest 进程 env → e2e `BlueprintEditor()` 优先读 env 拿到错误 token → 真实 UE Bridge HTTP 403（8/23 挂起 403 的真根因）→ e2e fixture 改为**显式直读 `%APPDATA%/ue5_bridge/token`**（`_resolve_real_bridge_token()`，免疫 env 污染；优先级：token 文件直读 > 硬编码 header > 依赖 env）
> - **📋 C++ API 清单同步 v0.9**：§2.3 从「21 API」修正为 **27 API**（补 v0.8-v0.10 新增 6 个：AddComponentToBlueprint / SetComponentProperty / ReadVariables / BreakPins / RemoveNode / ExecutePythonOnGameThread；DLL 163KB → 437KB；能力矩阵补 v0.9 列 + 「组件添加+属性设置」「程序化读取变量」2 行）
> - **📄 裁决语义补录**：C++ bool + `FString& OutError` 反射规则（**''=成功 / None=失败无文本 / 非空 str=失败+错误文本**）——`add_component` / `set_component_property` 实测对齐
> - **🔌 OutError 补丁**：DLL 437,248B（8/23 01:52）含 OutError 补丁（重名 add 返回非空错误文本、严格拒绝，见 ue5_bridge.py `_cmd_add_component` 注释）；部署于 `Binaries\Win64`，旧版备份 `.bak5`（备份链 .bak~.bak5 完整）
> - **🧪 CPP-APIS 在线回归固化（T-20260908 阶段③）**：新增 `tests/test_cpp_apis_online.py` 2 个 `ue_online` 用例（① read_variables 真实清单 Health/MaxHealth 假阳性消除 ② add_component + properties BoxExtent=[200,100,50] → SetComponentProperty → spawn 实测精确生效；token 直读 fixture 免疫 env 污染，模式同 test_blueprint_editor_e2e.py）——离线自动 skip（无需 mock 即可收集通过）；`run_tests.bat e2e/online --ue-online` 已纳入（e2e 批 38 → 40 用例）。将来 DLL/模板改动后**在开发端**跑 `run_tests.bat e2e --ue-online` 即自动回归这 2 个 C++ API 在线路径（第三方发布包不含 `tests/` 与 `run_tests.bat`，此命令仅开发端可用）
>
> ### v0.8.1 更新日志（T-20260806-UE5AUTOMATION-FIX-AND-VERIFY · 测试缺陷修复）
> - **🧹 清理 + docstring 核对（2026-08-20 T-20260820-SKILL-CLEANUP）**：① 删除 scripts/ 23 个临时探针脚本 + tests/ 60+ 个 `_*_tmp.*` 排查残留（2026-08-05 调试遗留）② `ue5_bridge.py` `delete_node` / `_delete_node_on_game_thread` docstring 从过时「三级降级链（graph.remove_node → remove_graph_node → destroy_node）」核对为实测实现（C++ BlueprintPythonBridge.remove_node 优先 + remove_unused_nodes 降级；三个旧 API 在 UE 5.1 Python 绑定实测不存在）——与 T-20260820-REMOVE-NODE-KB 经验条目一致
> - **🚀 从零 build → 运行（PIE）端到端实测闭环（2026-08-20 T-20260820-UE5BRIDGE-VERIFY-RUN）**：此前「从零生成蓝图 → 编译 → PIE 运行」链路**从未在线实测过**（验证盲区，2026-08-20 诚实汇报后获用户授权实测）
>   - **实测证据链（UE 在线 · dev-project / 5.1.1）**：① `POST /build` 从零生成 BP_Verify_Run5（Actor + VerifyFlag 变量 + 事件开始运行 + 打印字符串 + 连线 + 编译 Status=3）→ ② `spawn_actor_from_class` 进关卡 → ③ Simulate/PIE 注入（`ue_send.py` UDP 注入 `editor_play_simulate`，bridge 无 PIE 端点）→ ④ 日志 `LogBlueprintUserMessages: [BP_Verify_Run5_C_UAID_...] BP_Verify_Run5 RUNNING!` = **BeginPlay 触发 + PrintString 输出成功** → ⑤ 清理
>   - **🐛 实测暴露 3 处模板 bug（`game_thread_build.py.tmpl`）**：① `add_event_node` 传参错误（传 bp 应为 graph）；② `add_function_call_node` 缺参（A' 修复只改了 `_cmd_add_function_node` 未同步到模板）；③ `create_actor_blueprint` 已默认创建 BeginPlay 等事件，模板 existing 检查用英文标题找不到中文标题 → 重复添加 → 编译失败。均已修复 + BP_Verify_Run5 重建验证通过
>   - **⚠️ 实测经验**：① Actor 蓝图事件函数名是 `ReceiveBeginPlay`（非 BeginPlay）；② 节点标题是本地化中文（「事件开始运行」「打印字符串」），连线/查找必须用中文标题；③ Simulate（SIE）不触发 BeginPlay，且 PIE/Simulate 期间 UDP remote execution 通道失效——PIE 触发需先清残留坏蓝图（编译失败会弹「蓝图编译错误」模态框阻塞 UDP 与 PIE 启动）；④ 客户端 JSON 解析长输出会截断（Unterminated string），需分步短输出执行
> - **🐛 Fix 1**：`fake_unreal.BlueprintPythonBridge` 补 `remove_node` API → `test_delete_node_success` 恢复 passed（真根因：fake 缺 RemoveNode，此前验证报告误判为 FakePin 缺 linked_to）
> - **🐛 Fix 2**：marker 审计豁免 `ue_offline` → `test_ue_calling_tests_marked_online` 恢复 passed（3 个离线韧性测试不再误判违规）
> - **⏱️ Fix 3**：`run_tests.bat`（开发端）`--timeout` 30→90s → `test_ue_send_cli_no_ue`（需 30-60s）不再被误杀中断整个 session
> - **🔤 Fix 4**：`run_tests.bat`（开发端）改写为**纯 ASCII 英文注释版** → cmd 任意代码页稳定解析（不再需转码副本；UTF-8 BOM/`chcp 65001`/`PYTHONIOENCODING` 方案实测均失败——cmd 解析碎片化/中文注释卡死）
> - **📄 Fix 5**：版本体系统一为 **v0.8.1**（SKILL.md / workflow.md / DEPLOY.md 三文档对齐）
> - **🔧 Phase 2 修复（T-20260806-UE5BRIDGE-CRASH-FIX · 异步崩溃 4 次复发修复）**：ue5_bridge asyncio B+D+E+G（9 端点 async→sync + `_gt_worker` 异常字符串化 + trace_id middleware）——v0.8.1 修复 2 项缺陷后 + 异步崩溃 4 次复发修复
>   - 离线基线：116 passed · 5 skipped · 0 failed（2026-08-06 复跑确认；2026-08-20 复跑 116 passed · 5 skipped · 0 failed · 51.16s 一致）
>   - ✅ **UE 在线 8 项全 PASSED（2026-08-20 实测）**：analyzer_phase2 3 项（phase2a/b/d）+ blueprint_e2e 4 项（update_node_param / connect_pins / disconnect_pin / delete_node · 22.80s）+ mermaid l2_online 1 项（首次实跑 PASSED 1.24s）——8/06 asyncio 崩溃后 B+D+E+G 修复完成，缺陷1/2、SET-FIX、LEFTOVER-EXEC/TAIL 均闭环，UE 零崩溃
>   - 验证文档：`docs/VERIFICATION-PHASE2.md`（含 3 轮复跑清单 + 判定标准 + BP_HelloMirror InString 回归检查）
>
> ### v0.8 更新日志（T-20260805-UE-FULL-CLOSURE · 真验证闭环）
> - **🔍 bridge_loaded 真验证**：`ue_send.py --bridge-diag` 改为真实加载探针（① import BlueprintPythonBridge 模块 ② unreal.BlueprintPythonBridge C++ 属性 ③ get_all_graphs/list_nodes 实测），消除"只看目录存在"的误判
> - **✅ Phase 2 端到端 PASSED**：真实 UE（dev-project / UE 5.1.1）上 BP_HelloMirror 走通 ue_send 提取 → analyzer feed → JSON 输出全链路（`tests/test_analyzer_phase2.py`）
> - **🧪 测试体系扩展**：新增 `test_blueprint_editor_e2e.py`（4 个真实 UE E2E：update_node_param / connect / disconnect / delete，前后回滚）/ `test_analyzer_degrade.py`（JSON 降级容错 L0-L3）/ `test_ue_send_resilience.py`（断连兜底）/ `test_token_auth.py`（4 端点 wrong/no token → 403 契约）/ `test_marker_consistency.py`（ue_online 标记审计）
> - **📦 run_tests.bat**（开发端）：新增 `blueprint_editor` / `e2e` 模式（保留原 6 模式）
> - **🛠️ 容错增强**：`analyzer.py` 对 basic_info=null / graphs=null 畸形输入兜底（L3 降级不崩）
> - **🧪 测试状态**：✅ 100 passed, 1 skipped（baseline）→ v0.8 新增 15+ 项全绿；v0.8.1 修复 2 项缺陷后 116 passed · 5 skipped · 0 failed（Phase 1 实测 2026-08-06）。Phase 2 UE 在线实测（2026-08-06）：`test_analyzer_phase2.py` 3 项 E2E 解锁 **全 PASSED**（此前 skip）；`test_blueprint_editor_e2e.py` 4 项**未解锁**——UE 运行中 uvicorn asyncio 线程 ACCESS VIOLATION 崩溃（`EXCEPTION_ACCESS_VIOLATION`，python39+_asyncio 调用栈，20:59:55），`test_e2e_update_node_param` 写入后 verify/回滚阶段崩、其余 3 项 setup 前 UE 已退出（1 failed + 3 errors）。根因与 ue5_bridge 线程生命周期相关（同 T-20260805-CRASH-ATEXIT-FIX 家族），**待 UE 稳定性修复后复测** → **Phase 2 已修复（B+D+E+G）**，✅ **2026-08-20 全部复跑通过：UE 在线 8/8 PASSED**（analyzer_phase2 3/3 + blueprint_e2e 4/4 单次套件 22.80s + mermaid l2_online 1/1 首次实跑）——按 `docs/VERIFICATION-PHASE2.md` §三 复跑验证闭环
>
> ### v0.7 更新日志（T-20260727-UE05-V07 · 增量修改模式）
> - **🎯 新模式**：§3.5 增量修改模式——4 个核心 API：`update_node_param` / `connect_pins_by_id` / `disconnect_pin` / `delete_node`
> - **📦 新模块**：`blueprint_editor.py`（~487 行）—— HTTP Bridge 上层封装，含 freeze→execute→verify 三步保护 + 事务日志
> - **🔌 新端点**：`ue5_bridge.py` 新增 `/disconnect`（断线，GameThread 安全）+ `/delete_node`（删节点，含引用检查）
> - **📋 能力矩阵**：v0.7 列新增「蓝图续写」「增量修改」「节点删除」「断线/重连」
> - **🛡️ 安全**：内建 pitfalls.md 9 坑防护 + KB 命中「跨图同属校验」
- **🧪 测试状态**：离线 Mock 测试 ✅（17/17 通过 · `tests/test_blueprint_editor_mock.py`）| 端到端测试 ✅ 100 passed, 1 skipped (v0.8)
- **🐛 T-20260727-UE05-V07-FIX**：修复 disconnect_pin 对 catalog_only 降级的误判（success=False + warnings）+ 补齐离线测试

---

## 一、技能概述

### 1.1 一句话定位

**面向 UE5.1 的多智能体自动化管线** — 读取蓝图、自动编写蓝图、日志报错诊断、项目架构评估，四合一开发辅助工具。

### 1.2 四类场景

| 场景 | 你只需要 | 你会得到 |
|:---|:---|:---|
| **🔍 蓝图资产梳理** | 给一个蓝图路径 | 完整分析报告：变量清单、节点拓扑、Mermaid 流程图、引用资产、关联蓝图、优化建议 |
| **🏗️ 快速搭建原型** | 用自然语言描述功能 | 自动创建蓝图骨架（资产 + 变量 + 组件 + 接口），编译验证通过 |
| **🩺 报错排查** | 贴一段 UE 日志 | 根因定位 + 修复方案 + 可自动修复项直接修复 |
| **📐 架构评审** | 给项目根目录 | 全项目扫描 → 架构评估报告 + 按严重程度排序的问题清单 |

### 1.3 谁在背后工作

```
用户 ──→ maintainer（总调度） ──→ 判定模式 ──→ 分派任务
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
         dev（引擎交互）  vision（截图识别）  docs-writer（报告输出）
         Python/Bridge    图像/OCR后备    文档/格式化
```

---

## 二、前置依赖

### 2.1 你必须具备的环境

| 依赖项 | 版本/说明 | 检查方式 |
|:---|:---|:---|
| **Unreal Engine** | **5.1.x 开箱即用**（随包 DLL 实测 5.1.1）；5.2–5.5 需重编译 bridge；5.6+ 未验证 | `python scripts\check_ue_version.py`（30 秒只读自检） |
| **UE Python 插件** | `Python Editor Script Plugin` 已启用 | UE 菜单 → Edit → Plugins → 搜索 "Python" → 确认已勾选 |
| **Remote Execution** | `Editor Preferences → Python → Enable Remote Execution` 已勾选 | 不依赖固定端口，使用 UDP 多播自动发现 |
| **ue_send.py** | Agent 与 UE 的通信脚本 | 放在 workspace/scripts/ 或通过 `UE_SEND_PATH` 指定 |
| **C++ Bridge 插件** | 解锁蓝图节点级操作（✅ 已就绪 · DLL 901,120 B（2026-09-18 编译）· 源码实测 59 UFUNCTION / health 实测 bridge_apis 80 项） | 现已可用；`output/BlueprintPythonBridge/` 完整包（DLL+PDB+头文件+uplugin） |

### 2.2 Bridge 通信架构（混合通道）

```
┌──────────────────────────────────────────────────────────┐
│            Agent 侧                │     UE5 侧           │
│                                    │                      │
│  ┌─ 读通道 ──────────────────────────────────────────┐  │
│  │ ue5_runner.py read/health                          │  │
│  │   → ue_send.py ── UDP 多播发现 ──▶ Remote Execution│  │
│  │     (不阻塞 GameThread, 适合读取/查询)              │  │
│  └────────────────────────────────────────────────────┘  │
│                                    │                      │
│  ┌─ 写通道 ──────────────────────────────────────────┐  │
│  │ ue5_runner.py build/connect                        │  │
│  │   → HTTP POST ────────────────▶ ue5_bridge.py      │  │
│  │     (FastAPI :8889, 后台运行, 不卡 GameThread)      │  │
│  │     → unreal Python API + BlueprintPythonBridge    │  │
│  │     (创建资产 / 添加节点 / 连线 / 编译)             │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

> **混合通道说明：** 
> - **读/健康检查** → `ue_send` (UDP) 直发 Python 命令，轻量快速，不启动服务。
> - **写/创建/连线** → `ue5_bridge` HTTP 服务 (端口 8889)。`ue5_runner.py build` 自动检测并远程启动 bridge（通过 `ensure_http_bridge()`），无需手动启动。
> - **为什么分通道：** ue_send 基于 `remote_execution`，写操作可能阻塞 GameThread 导致 UE 卡顿。HTTP bridge 在后台线程运行，写入操作通过 tick worker 异步调度，不影响编辑器响应。

### 2.3 C++ Bridge API 清单（全 59 项 · 随包 `.h` 源码实测 59 UFUNCTION · DLL 901,120 B）

> **来源：** `<Project>\Plugins\BlueprintPythonBridge\Source\BlueprintPythonBridge\Public\BlueprintPythonBridge.h`（开发机工程路径已占位化）· SKILL-06 编译验证 + v0.8-v0.10 新增补齐（T-20260805 / T-20260822 / T-20260823）· **2026-09-12 复核**：随包 `.h`（21,619 B）实测 UFUNCTION = 59
> **口径：** 下表 1-21 为 SKILL-06 原始 21 API；22-27 为 v0.8-v0.10 新增（实测已入 DLL）。随包 DLL（901,120 B · 2026-09-18 编译）源码实测 UFUNCTION = **59** → （已由新节覆盖）；**28-59 项已于 2026-09-12 逐条补全**（T-20260912-RELEASE-V29-CLEAN），本表现覆盖随包 `.h` 全部 59 个 UFUNCTION，无遗漏项；能力判定仍以 `bridge_apis` health 实测 + 源码 `.h` 为准。

#### 蓝图资产管理

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 1 | `CreateActorBlueprint` | `(AssetPath, AssetName) → UBlueprint*` | 创建 Actor 蓝图资产 |
| 2 | `CompileBlueprint` | `(UBlueprint*) → bool` | 编译蓝图 |
| 3 | `CompileAndSave` | `(UBlueprint*) → bool` | 编译并保存蓝图 |
| 4 | `AddMemberVariable` | `(Blueprint, VarName, VarType) → bool` | 添加成员变量 |

#### 图表管理

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 5 | `FindEventGraph` | `(UBlueprint*) → UEdGraph*` | 查找 EventGraph（纯查询） |
| 6 | `GetEventGraph` | `(UBlueprint*) → UEdGraph*` | 获取或创建 EventGraph |
| 7 | `CreateEventGraph` | `(UBlueprint*) → UEdGraph*` | 创建空 EventGraph |
| 8 | `GetAllGraphs` | `(UBlueprint*) → TArray<UEdGraph*>` | 获取所有图表 |

#### 节点添加

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 9 | `AddEventNode` | `(Graph, EventName, X, Y) → UEdGraphNode*` | 添加事件节点 |
| 10 | `AddFunctionCallNode` | `(Graph, FuncName, Class, X, Y) → UEdGraphNode*` | 添加函数调用节点 |
| 11 | `AddBranchNode` | `(Graph, X, Y) → UEdGraphNode*` | 添加 Branch 控制流节点 |
| 12 | `AddSequenceNode` | `(Graph, NumOutputs, X, Y) → UEdGraphNode*` | 添加 Sequence 控制流节点 |

#### 节点读取

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 13 | `ListNodes` | `(UEdGraph*) → TArray<FBPNodeInfo>` | 列出图中所有节点 |
| 14 | `ListPins` | `(UEdGraphNode*) → TArray<FBPPinInfo>` | 列出节点所有引脚 |
| 15 | `GetPinInfos` ⭐ | `(Graph, FBPNodeInfo&) → TArray<FBPPinInfo>` | 通过 FBPNodeInfo 远程查询引脚 |
| 16 | `FindNodeByTitle` | `(Graph, Title) → UEdGraphNode*` | 按标题精确查找节点 |

#### 连线操作

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 17 | `ConnectPins` | `(SrcNode, SrcPin, DstNode, DstPin) → bool` | 连接两个引脚 |
| 18 | `SetPinDefaultValue` | `(Node, PinName, Value) → bool` | 设置引脚默认值 |
| 19 | `GetNodeConnections` ⭐ | `(UEdGraph*) → TArray<FBPConnectionInfo>` | 读取全图连线拓扑 |
| 20 | `GetNodeConnectionsForNode` ⭐ | `(Graph, FBPNodeInfo&) → TArray<FBPConnectionInfo>` | 读取单节点所有连线 |

#### 图维护

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 21 | `ClearGraphNodes` | `(UEdGraph*) → void` | 清除图中所有节点 |

#### v0.8-v0.10 新增 API（清单同步补齐）

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 22 | `BreakPins` | `(SrcNode, SrcPin, DstNode, DstPin) → bool` | 断开两引脚间连线（v0.8 T-20260805：UE5.1 Python 无 EdGraphPin 绑定，只能 C++ 实现） |
| 23 | `RemoveNode` | `(Graph, Node) → bool` | 从图中移除节点（v0.8 T-20260805：Python 无 RemoveNode 绑定；Python delete_node 优先走此 API + remove_unused_nodes 降级） |
| 24 | `AddComponentToBlueprint` | `(Blueprint, CompName, CompClass, OutError&) → bool` | 给蓝图添加组件（v0.9 T-20260822：C++ SCS API，绕过 UE5.1 Python 无 SimpleConstructionScript 暴露限制。反射语义见下注①） |
| 25 | `SetComponentProperty` | `(Blueprint, CompName, PropName, PropValue, OutError&) → bool` | 设置组件属性（v0.10 T-20260823：SCS 节点模板 + FindFProperty + ImportText_Direct，替代 Python BPGC.ComponentTemplates 不可达路径。见下注①） |
| 26 | `ReadVariables` | `(UBlueprint*) → TArray<FBPVariableInfo>` | 读取蓝图变量清单（v0.10 T-20260823：C++ 反射 NewVariables + GeneratedClass 兜底，消除 Python new_variables 假阳性/降级。返回 FBPVariableInfo：VariableName/VariableType/DefaultValue/Category/bIsEditable） |
| 27 | `ExecutePythonOnGameThread` | `(Code, OutResult&) → void` | 在 GameThread 上执行任意 Python 代码（线程安全入口，供 /read 等非 GameThread 端点调用） |

> **注① · C++ bool + OutError 反射语义（裁决 A · 实测对齐）**：C++ 签名 `(bool, FString& OutError)` → UE Python 反射规则：`bool=false` → 返回 `None`（OutError 被吞）；`bool=true` → 返回 OutError 值（成功 `''` / 失败非空错误文本）→ **`''`=成功 / `None`=失败无文本 / 非空 str=失败+错误文本**。`add_component`（含 properties 逐属性 `set_component_property`）已按此语义解析。

#### 全量补齐：节点 / 事件 / 引脚 / 类级 API（28-59 · 2026-09-12 补全）

**节点通用操作**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 28 | `AddNodeByClass` | `(Graph, NodeClass, X, Y) → Node*` | 按 `TSubclassOf<UEdGraphNode>` 通用建节点（任意节点子类） |
| 29 | `SetNodePosition` | `(Node, X, Y) → bool` | 设置节点坐标（NodePosX/NodePosY + NotifyGraphChanged） |
| 30 | `GetNodeByGuid` | `(Graph, GuidStr) → Node*` | 按 NodeGuid 精确查找节点（解同名节点接错线的歧义） |
| 31 | `SetNodeProperty` | `(Node, PropName, Value) → bool` | 反射写节点属性（EnabledState / NodeComment / bCommentBubbleVisible / AdvancedPinDisplay 等） |

**变量与转换节点**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 32 | `AddVariableNode` | `(Graph, VarName, bIsSetter, X, Y) → Node*` | 变量 Get/Set 节点（走 SetFromProperty，不手拼 FMemberReference） |
| 33 | `AddCastNode` | `(Graph, TargetClassName, X, Y) → Node*` | 动态 Cast（类名支持 `/Script/Engine.Actor` 与 `/Game/.../BP_X.BP_X_C`） |
| 34 | `AddClassCastNode` | `(Graph, TargetClassName, X, Y) → Node*` | Class 动态 Cast（`UK2Node_ClassDynamicCast`，对类引用 Cast） |

**控制流 / 结构 / 表达式**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 35 | `AddMacroNode` | `(Graph, MacroName, X, Y) → Node*` | 标准宏实例（ForEachLoop / ForEachLoopWithBreak / WhileLoop / IsValid / Gate / DoOnce / MultiGate / Sequence / FlipFlop …） |
| 36 | `AddSwitchNode` | `(Graph, TypeKind, EnumOrTypeName, X, Y) → Node*` | Switch 节点（TypeKind ∈ `int`/`string`/`name`/`enum`；enum 时用 EnumOrTypeName 指定枚举） |
| 37 | `AddStructNode` | `(Graph, StructName, bIsBreak, X, Y) → Node*` | 结构体 Make/Break（Vector / Rotator / Transform / HitResult …） |
| 38 | `AddMathExpressionNode` | `(Graph, Expression, X, Y) → Node*` | 数学表达式节点（如 `(A+B)*C`；ReconstructNode 内触发 RebuildExpression） |

**输入事件**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 39 | `AddInputKeyEventNode` | `(Graph, KeyName, X, Y) → Node*` | 按键事件（W / A / SpaceBar / LeftMouseButton / RightMouseButton …） |
| 40 | `AddInputActionEventNode` | `(Graph, ActionName, X, Y) → Node*` | Legacy InputAction 事件（Enhanced Input 走本接口需实测，不可用降级为 39 按键事件） |
| 41 | `AddInputAxisEventNode` | `(Graph, AxisName, bIsKeyAxis, X, Y) → Node*` | 轴输入事件（`bIsKeyAxis=true` → InputAxisKeyEvent(FKey)；false → InputAxisEvent(FName)） |

**事件绑定**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 42 | `AddComponentBoundEventNode` | `(Graph, ComponentName, DelegateName, X, Y) → Node*` | 组件委托事件（`K2Node_ComponentBoundEvent`；电梯门 OnComponentBeginOverlap 走此接口） |
| 43 | `AddActorBoundEventNode` | `(Graph, ActorName, DelegateName, X, Y) → Node*` | 绑定关卡 Actor 实例事件（InitializeActorBoundEventParams；与 42 组件委托语义不同） |
| 44 | `AddCustomEventNode` | `(Graph, EventName, X, Y, SignatureFuncName) → Node*` | 自定义事件（先设 CustomFunctionName 再走建节点顺序；可选委托签名函数全路径/短名） |
| 45 | `AddCreateDelegateNode` | `(Graph, FunctionName, X, Y) → Node*` | CreateDelegate（Bind Event；SetFunction(FName)） |
| 46 | `AddEventDispatcher` | `(Blueprint, DelegateName, OutError&) → bool` | 建事件分发器（PC_MCDelegate 成员 + DelegateSignatureGraphs；引擎无公开静态 API，按 FBlueprintEditor 路径自建） |

**引脚 / 函数图**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 47 | `AddPinToNode` | `(Node, Direction, PinTypeStr, PinName, OutPinName&) → bool` | 任意节点动态加引脚（Direction = input\|output；PinTypeStr 语法见 cpp `BridgeParsePinType`） |
| 48 | `RemovePinFromNode` | `(Node, PinName) → bool` | 按引脚名移除节点引脚（UEdGraphNode::RemovePin） |
| 49 | `TryNodeAddInputPin` | `(Node) → bool` | 节点语义层加输入引脚（内部 Cast<IK2Node_AddPinInterface>，如 Switch / Select / FormatText） |
| 50 | `AddFunctionPinEntry` | `(Graph, PinName, PinTypeStr, bIsInput) → bool` | 函数图参数引脚（bIsInput=true → FunctionEntry 入参；false → FunctionResult 出参） |
| 51 | `AddLocalVariable` | `(Graph, VarName, VarType) → bool` | 函数图局部变量（写 UK2Node_FunctionEntry::LocalVariables + 重建 + 标记结构修改） |

**高级节点**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 52 | `AddCommentNode` | `(Graph, Text, X, Y, Width, Height) → Node*` | 注释框（`UEdGraphNode_Comment`；类属 UnrealEd 模块、Python 不可达） |
| 53 | `AddTimelineNode` | `(Graph, TimelineName, X, Y) → Node*` | Timeline 节点（先 AddNewTimeline 建模板，再建节点；空名自动取唯一名） |
| 54 | `AddCompositeNode` | `(Graph, X, Y) → Node*` | 折叠图 Composite（PostPlacedNewNode 内建 BoundGraph + Tunnel 进出节点） |
| 55 | `AddAsyncActionNode` | `(Graph, FactoryFunc, FactoryClass, ActivateFunc, X, Y) → Node*` | 异步任务节点（ProxyFactory* 为 protected UPROPERTY，走反射写入） |
| 56 | `AddEnumLiteralNode` | `(Graph, EnumName, ValueName, X, Y) → Node*` | 枚举字面量节点（EnumName 全路径/短名；ValueName 可选，写入 Enum 引脚默认值） |

**类级 / 组件**

| # | API | 签名 | 用途 |
|:--|:---|:---|:---|
| 57 | `SetClassDefault` | `(Blueprint, PropName, Value) → bool` | 写类默认值（GeneratedClass CDO → FindFProperty + ImportText；解「PC 无实例」场景） |
| 58 | `RemoveComponent` | `(Blueprint, ComponentName, OutError&) → bool` | 移除 SCS 组件（语义对齐裁决 A：`''`=成功 / 非空=失败+错误文本） |
| 59 | `AddImplementedInterface` | `(Blueprint, InterfaceClassPath) → bool` | 蓝图实现新接口（FBlueprintEditorUtils::ImplementNewInterface） |

> ⭐ = SKILL-06 新增（3 个 API：`GetPinInfos`、`GetNodeConnections`、`GetNodeConnectionsForNode`）  
> 完整交付包：`output/BlueprintPythonBridge/`（DLL 901,120 B + PDB + .h + .uplugin）  
> 编译工具链：VS 2022 14.42 + UE 5.1 · `-NoParallelExecutor`（绕过 Win32Exception 998）

#### L2 Python 命令白名单（21 条 · 2026-09-12 在线实测 · 报告 §1 A 项）

> **来源（唯一权威）**：`output\T-20260912-UE5BRIDGE-PYWRAP-L2-ONLINE\L2-在线联调报告-20260912.md` §1「A 项 · 在线联调 21 项逐项表」（基线 = run_4 · `71 passed + 1 xfailed / 0 failed` · 72.42s）。
> **口径**：21 条 = `/command` 白名单的 **L2 批次**（白名单共 **72 条** = 15 基础 + 11 L1 + 21 L2 + 1 落盘 + 2 资产 + 9 v3.0能力 + 9 材质 + 4 UMG）；每条 = **≥1 正路径 + ≥1 fail-loud 负路径**（在线用例 `tests\test_pywrap_l2_online.py`，需 `--ue-online`）。**20 ✅ + 1 ⚠️(xfail) / 21**。
> **调用方式**：`POST http://127.0.0.1:8889/command` · 头 `X-Skill-Token: <token>` · 体 `{"command": "<命令名>", "params": {...}}`；未登记命令 → `Unknown command`（fail-loud，不执行任意代码）。
> **逐条详表**（完整签名 / 入参 / raw 锚点 / 风险注记）：`docs\L2-COMMANDS.md`；调度侧速查：`workflow.md` 附录 B。

| # | 命令 | 对应 C++ API | 关键入参 | 正路径（run_4 实测） | 负路径 fail-loud（run_4 实测） | 结论 |
|:--:|:---|:---|:---|:---|:---|:--:|
| 1 | `add_cast_node` | AddCastNode(#33) | bp_path, graph_name=EventGraph, target_class_name, node_pos | `title="类型转换为 Actor"` · 608ms | 不可解析全路径 → `拒绝调用引擎`；空值 → `参数为空`；图表不存在 → `图表未找到` | ✅ |
| 2 | `add_class_cast_node` | AddClassCastNode(#34) | bp_path, graph_name=EventGraph, target_class_name, node_pos | `title="类型转换为 Actor类"` · 594ms | 空值 → `参数为空` | ✅ |
| 3 | `add_macro_node` | AddMacroNode(#35) | bp_path, graph_name=EventGraph, macro_name, node_pos | `title="For Each Loop"` · 619ms；**24 候选宏名全扫 `ok=24 / fail=0`** | 未知宏名 → `返回 None（建节点失败）` | ✅ |
| 4 | `add_switch_node` | AddSwitchNode(#36) | bp_path, graph_name=EventGraph, type_kind, enum_or_type_name, node_pos | `int` → `切换整型`；`enum` → `切换ECollisionChannel` / `切换EInputEvent` | `type_kind 非法 'bogus'` | ✅ |
| 5 | `add_struct_node` | AddStructNode(#37) | bp_path, graph_name=EventGraph, struct_name, b_is_break, node_pos | `make` → `"创建Vector"`；`break` → `"中断Vector"` | 未知结构 → `返回 None`；空值 → `参数为空` | ✅ |
| 6 | `add_enum_literal_node` | AddEnumLiteralNode(#56) | bp_path, graph_name=EventGraph, enum_name, value_name, node_pos | `title="文字枚举ECollisionChannel"` ×2 | 空值 → `参数为空` | ✅ |
| 7 | `add_composite_node` | AddCompositeNode(#54) | bp_path, graph_name=EventGraph, node_pos | `title="EdGraph_0\n合并的图表"`（`pins` 回读为空 → 已留 warning，不静默） | 图表不存在 → `图表未找到` | ✅ |
| 8 | `add_math_expression_node` | AddMathExpressionNode(#38) | bp_path, graph_name=EventGraph, expression, node_pos | `title="(A + B) * C\n数学表达式"` · 653ms | 空表达式 → `参数为空`；图表不存在 | ✅ |
| 9 | `add_async_action_node` | AddAsyncActionNode(#55) | bp_path, graph_name=EventGraph, factory_function_name, factory_class_name, activate_function_name, node_pos | `title="延迟"` · 628ms（ProxyFactory 反射写入） | 不可解析类 → `拒绝调用引擎`；空类名 → `参数为空` | ✅ |
| 10 | `add_create_delegate_node` | AddCreateDelegateNode(#45) | bp_path, graph_name=EventGraph, function_name, node_pos | `title="创建事件"` · 590ms | 空 `function_name` → `参数为空` | ✅ |
| 11 | `add_actor_bound_event_node` | AddActorBoundEventNode(#43) | bp_path, graph_name=EventGraph, actor_name, delegate_name, node_pos | `title="Actor开始重叠时 (StaticMeshActor0)"` | 无该 Actor / 无该委托 → `返回 None`；空委托名 → `参数为空` | ✅ |
| 12 | `add_input_axis_event_node` | AddInputAxisEventNode(#41) | bp_path, graph_name=EventGraph, axis_name, b_is_key_axis, node_pos | 按键轴 `"W"`；命名轴 `"输入轴Move Forward / Backward"` | 非法键 → `返回 None`（**并自动清理残留**）；空 `axis_name` | ✅ |
| 13 | `add_input_action_event_node` | AddInputActionEventNode(#40) | bp_path, graph_name=EventGraph, action_name, node_pos | `title="输入操作Jump"` | 空 `action_name` → `参数为空` | ✅ |
| 14 | `add_component_bound_event_node` | AddComponentBoundEventNode(#42) | bp_path, graph_name=EventGraph, component_name, delegate_name, node_pos | `title="组件开始重叠时 (PywrapBox)"` | 无该组件 / 无该委托 → `返回 None` | ✅ |
| 15 | `add_pin_to_node` | AddPinToNode(#47) | bp_path, graph_name=EventGraph, node_guid, direction, pin_type, pin_name | `ok=true` | 同名引脚 → `引脚 'DupPin' 已存在…（禁静默双建）`；`direction 非法 'sideways'`；GUID 不命中 | ✅ |
| 16 | `remove_pin_from_node` | RemovePinFromNode(#48) | bp_path, graph_name=EventGraph, node_guid, pin_name | `ok=true · removed=true` | 无该引脚 → 报错并回读实读列表 | ✅ |
| 17 | `try_node_add_input_pin` | TryNodeAddInputPin(#49) | bp_path, graph_name=EventGraph, node_guid, strict | ⚠️ **XFAIL**（引擎层不支持：UE5.1 `K2Node_SwitchInteger` 未实现 `IK2Node_AddPinInterface`） | 同 unsupported 语义 fail-loud | ⚠️ xfail |
| 18 | `set_node_property` | SetNodeProperty(#31) | bp_path, graph_name=EventGraph, node_guid, property_name, value | `NodeComment` / `bCanRenameNode` 均 `ok=true` | GUID 不命中；未知属性 → `返回 False` | ✅ |
| 19 | `remove_component` | RemoveComponent(#58) | bp_path, comp_name | `ok=true`（`removed` 回读不可用 → `warnings[2]`，不静默） | 组件不存在 → `not found in SimpleConstructionScript` | ✅ |
| 20 | `add_implemented_interface` | AddImplementedInterface(#59) | bp_path, interface_class_path | 加固前实测 `ok=true` 330ms（D-1 族入口） | 不可解析全路径 → `拒绝调用引擎`；重复接口 → `返回 False`；🔴 **裸短名 = R2 硬崩 → R3 已硬化**（R5 真机复验：`BridgeError` 22ms · UE 未崩 · 合法裸短名不误拒） | ✅（含 R3 硬化） |
| 21 | `add_event_dispatcher` | AddEventDispatcher(#46) | bp_path, delegate_name | `ok=true` | 同名分发器 → `返回 None（错误文本被 UE 反射吞掉）`；空 `delegate_name` | ✅ |

> **小计**：**20 ✅ + 1 ⚠️(xfail) / 21**。⚠️ `try_node_add_input_pin` = 引擎层不支持（UE5.1 `K2Node_SwitchInteger` 未实现 `IK2Node_AddPinInterface`）→ 用例按 `xfail` 记录，调用方**不得依赖**该命令成功。
> 🔴 **风险注记（R3/R5 已硬化 · 已真机复验）**：`add_implemented_interface` 等 7 个「class/enum/struct/interface 路径入参」命令族，前置解析守卫已由「不可解析**全路径** → 拒绝调用引擎」扩展为「不可解析**裸短名** → 同样拒绝」（Tier B 硬化）。**引擎侧对不可解析路径会 `check()` 断言硬崩编辑器**（实测：`BP_PywrapL2Bare_20260912` 裸短名致 UnrealEditor 硬崩）→ 调用方必须传可加载路径（`/Script/Module.XXX` 或 `/Game/.../BPI_X.BPI_X_C`）。
> 📌 `read_nodes` 语义（R3）：`graph_name` 有值 → **只返回该图**；未指定/空串 → 全量（向后兼容）。

### 2.4 安全说明

**混合通道安全策略：**
- **读通道 (ue_send)**：走 UE `PythonScriptPlugin` 的 Remote Execution —— **UDP 多播发现 + 命令通道，作用域为同一局域网网段，协议本身无鉴权**（并非仅本机进程间通信）。同网段内任何开启 Remote Execution 的 UE 编辑器都可能被发现与驱动；隔离环境使用，或在防火墙限制 UDP 多播端口。命令执行范围受 `ue5_runner.py` 提供的白名单函数约束（`ue_send.py` 底层本身不设白名单）。
- **写通道 (HTTP bridge)**：仅监听 `127.0.0.1:8889`，不对外暴露。需 `X-Skill-Token` 头认证 —— **token 无"默认值 test123"**：bridge 首次启动时随机生成并落盘 `%APPDATA%\ue5_bridge\token`（环境变量 `UE5_BRIDGE_TOKEN` 可覆盖，env 优先于文件）；调用方经 `ue5_runner` 自动读取，手动调用时读该文件。CORS 仅允许本地来源。
- **⚠️ T-20260727-C03 · Mermaid 渲染安全**：`mermaid_render.py` 的 L2 在线渲染（`allow_external=True`）会将 Mermaid 代码通过 HTTPS GET 发送到第三方服务 `mermaid.ink`。若代码含项目敏感信息（蓝图函数名、类名、架构细节），存在数据外泄风险。**生产环境建议只使用 L1 (mmdc) 本地渲染**，确保 `allow_external=False`（默认值，已安全）。

> **Agent 侧注意：** Python 调用示例：
> ```python
> from ue5_runner import UE5Runner
> runner = UE5Runner()
> 
> # 读取（走 ue_send）
> runner.read("/Game/Blueprints/BP_Test")
> runner.health()
> 
> # 创建/连线（走 HTTP bridge，自动检测并启动）
> runner.build({...})      # 蓝图创建 + 变量 + 事件 + 函数 + 连线 + 编译
> runner.connect(...)       # 引脚连线
> ```

### 2.5 部署清单（首次使用）

> 🚀 **新机器/新宿主机首选 `scripts/init.py` 薄执行器初始化**（默认 dry-run 只读；产出**建议稿**须人工确认才生效）：
> - 第 1 步 探测预览：`python scripts/init.py`（dry-run 只读，不落盘）
> - 第 2 步 落盘建议稿：`python scripts/init.py --apply`
>   - 自动能力探测（list_agents API → agent.json → 系统提示词三级链）
>   - 自动定位 UE 引擎/项目 + 校验 Bridge 插件源（deploy.py 复用）
>   - 产出 `output/role_assignments.yaml`（角色分配）+ `output/init_report.md`（health 自检）
>   - 自动语义分配 → `status: proposed`（**建议稿，未生效**）
> - 第 3 步 **人工确认门**（建议稿生效为 confirmed 二选一）：
>   - `python scripts/init.py --apply --assign <agent_id>=<role> ...`（显式指派核心角色）
>   - 或手编 `output/role_assignments.yaml` 后 `python scripts/init.py --reconcile --apply`
> - ℹ️ single_agent 环境（≤1 enabled agent）确定性指派宿主 `all_in_one` → 自动 confirmed，无需人工通道
> - 非破坏：不删文件、不建自启服务；详细见 DEPLOY.md「新机器初始化」章节

| 步骤 | 操作 | 负责人 |
|:---|:---|:---|
| 0 | 初始化：`python scripts/init.py`（dry-run 预览）→ `python scripts/init.py --apply` 落盘建议稿（proposed）→ 人工确认门：`--assign` 或手编 yaml 后 `--reconcile --apply` 生效（confirmed） | maintainer自动执行 |
| 1 | 确认 UE 已安装（**随包桥 DLL 仅实测 5.1.x**；其他版本先跑 `python scripts/check_ue_version.py` 并见 `UE_VERSION_GUIDE.md`），`Python Editor Script Plugin` 已启用 | 你 |
| 2 | 确认 `Editor Preferences → Python → Enable Remote Execution` 已勾选 | 你 |
| 3 | 连通性测试：`python scripts/ue5_runner.py health` | maintainer自动执行 |
| 4 | 部署 Bridge 插件：init 报告 verify=通过时可 `--apply` 自动部署；否则按 DEPLOY.md 手动拷贝/编译 | 你/maintainer |
| 5 | 重启 UE5 编辑器，验证插件已加载（Edit → Plugins → 搜索 "BlueprintPythonBridge"） | 你 |

---

## 三、四种模式入口

### 3.1 分析模式 — 读懂一个蓝图

**触发词：** `分析蓝图 {路径}` / `看看 {蓝图名}` / `帮我梳理 {路径}`

**参数：**

| 参数 | 必填 | 说明 | 示例 |
|:---|:---|:---|:---|
| `blueprint_path` | ✅ | 蓝图资产路径（Content Browser 右键 → Copy Reference） | `/Game/Blueprints/BP_EnemyController` |
| `depth` | ❌ | 分析深度：`basic` / `full`（默认 full） | `basic` 只出基本信息+变量清单 |
| `diagram_format` | ❌ | 流程图格式：`mermaid`（默认）/ `ascii` | 不支持 Mermaid 渲染时切换 ascii |

**工作流：**

```
你：分析蓝图 /Game/Blueprints/BP_MyActor
      │
      ▼
maintainer ──→ dev：L1 unreal API 读取类信息、变量、编译状态
      │          L2 C++ Bridge 读取节点/连线（Bridge API 覆盖 · 59 UFUNCTION）
      │
      ▼
docs-writer ──→ 按模板输出分析报告（7 个章节）
      │
      ▼
maintainer ──→ 汇总呈现给你
```

**输出物：** `📊 {蓝图名} 蓝图分析报告.md`（详见 [输出物说明](#四输出物说明)）

---

### 3.2 创建模式 — 搭一个蓝图骨架

**触发词：** `创建一个蓝图用于 {用途}` / `帮我搭 {功能描述}` / `新建 BP {描述}`

**参数：**

| 参数 | 必填 | 说明 | 示例 |
|:---|:---|:---|:---|
| `description` | ✅ | 自然语言功能描述 | "可拾取物品，含拾取半径、物品类型枚举、拾取粒子特效" |
| `blueprint_type` | ❌ | 蓝图类型：`Actor`（默认）/ `Widget` / `FunctionLibrary` / `Component` | `Widget` |
| `parent_class` | ❌ | 父类，默认 AActor | `APawn` |
| `interfaces` | ❌ | 实现的接口列表 | `IInteractable, IDamageable` |

**工作流：**

```
你：创建一个可拾取物品蓝图，含拾取半径、物品类型枚举、拾取粒子特效
      │
      ▼
docs-writer ──→ 理解需求 → 输出流程方案（Step 1/2/3...）
      │
      ▼
你：确认 / 调整方案
      │
      ▼
dev ──→ HTTP → UE5：
          ✅ 创建蓝图资产
          ✅ 添加变量（含类型、默认值、Category）
          ✅ 添加组件
          ✅ 设置父类 / 接口
          ✅ 编译验证
      │
      ▼
maintainer ──→ 汇报结果 + 蓝图路径
```

**确认点：** 方案输出后**必须等你确认**才会动手创建，不会擅自操作 UE5 编辑器。

**能力边界：**

| v0.1 能做 ✅ | v0.1 暂不能 ❌（C++ Bridge 就绪后解锁） |
|:---|:---|
| 创建蓝图资产（Actor/Widget/FunctionLib） | 自动连接节点（Pin 连线） |
| 添加变量（类型+默认值+Category+Expose） | 创建自定义事件/函数图 |
| 添加组件（Component） | 从已有蓝图复制节点逻辑 |
| 设置父类、实现接口 | — |
| 编译验证，返回编译结果 | — |

**输出物：** `🏗️ {蓝图名} 创建方案.md`（方案阶段）+ 创建结果汇报（执行阶段）

---

### 3.3 诊断模式 — 查一个报错

**触发词：** `这个报错怎么回事` / `日志报错` / `帮我看看这段log` （后面贴日志）

**参数：**

| 参数 | 必填 | 说明 | 示例 |
|:---|:---|:---|:---|
| `log_text` | ✅ | UE 日志片段（支持多行） | 直接从 Output Log 复制粘贴 |
| `auto_fix` | ❌ | 是否自动修复：`true` / `false`（默认 true） | `false` 时仅诊断不修复 |

**工作流：**

```
你：贴日志
      │
      ▼
dev ──→ 解析日志：提取错误码、堆栈、关联蓝图/资产路径
      │
      ▼
dev ──→ 匹配错误模式库（error_patterns.json）
      │
      ├── 可自动修复（如缺引用、变量名拼写）
      │      └──→ dev直接修复 → 汇报修改了什么
      │
      └── 需人工判断
             └──→ docs-writer输出诊断报告（根因+建议+操作步骤）
```

**覆盖的错误类型（v0.1，15+ 模式）：**

| 类别 | 示例 | 自动修复？ |
|:---|:---|:---|
| 引用丢失 | `Referenced asset is null`、Missing Asset | ✅ |
| 编译错误 | `Blueprint could not be compiled`、Pin type mismatch | ⚠️ 诊断+建议 |
| 循环依赖 | 蓝图 A → B → A | ⚠️ 诊断+拓扑分析 |
| 接口不匹配 | Interface function not implemented | ✅ 补全签名 |
| 节点错误 | Cast failed、Node deprecated | ⚠️ 诊断+建议 |
| 性能告警 | Tick 耗时过长 | ⚠️ 诊断+优化建议 |

**输出物：** `🩺 诊断报告_{关联蓝图名}.md`

---

### 3.4 优化模式 — 评估整个项目

**触发词：** `分析项目 {路径}` / `项目架构评估` / `项目体检`

**参数：**

| 参数 | 必填 | 说明 | 示例 |
|:---|:---|:---|:---|
| `project_path` | ✅ | 项目 Content 目录 | `/Content/MyGame` |
| `scan_depth` | ❌ | 扫描深度：`quick` / `full`（默认 full） | `quick` 仅统计数量+TOP20热力图 |

**工作流：**

```
你：分析项目 /Content/MyGame
      │
      ▼
dev ──→ analyzer.py 扫描项目：
          ├── 遍历所有蓝图资产
          ├── 统计类型分布
          ├── 分析引用关系（热力图）
          └── 计算指标（复杂度/耦合度/继承深度/接口使用率）
      │
      ▼
docs-writer ──→ 输出架构评估报告：
          ├── 整体结构（数量+分布）
          ├── 架构指标（复杂度+耦合度+继承+接口）
          └── 问题清单（🔴阻断 🟡警告 🟢建议）
```

**输出物：** `📐 {项目名} 架构评估报告.md`

---

### 3.5 增量修改模式 — 编辑已有蓝图

**触发词：** `修改蓝图 {路径} {操作}` / `给 {蓝图} 加一个 {节点}` / `删除 {蓝图} 的 {节点}` / `把 {A} 连到 {B}`

**v0.7 新增。** 在已有蓝图上执行节点级编辑操作，不需要重新生成整个蓝图。

**核心 API（4 个）：**

| API | 用途 | 示例 |
|:---|:---|:---|
| `update_node_param(blueprint_path, node_id, param_name, new_value)` | 修改节点引脚默认值 | 改 PrintString 的 InString 为 "Hello" |
| `connect_pins_by_id(node_a, pin_a, node_b, pin_b)` | 按标识连接两个引脚 | BeginPlay.then → PrintString.execute |
| `disconnect_pin(node_id, pin_name)` | 断开引脚全部连线 | 断开 PrintString.InString 的输入 |
| `delete_node(node_id)` | 删除节点（含引用检查） | 删除一个废弃的 Delay 节点 |

**通信架构（增量修改专有）：**

```
Agent ── HTTP ──▶ ue5_bridge.py (UE 编辑器内 :8889)
                    ├── /read       → 读取当前状态（快照）
                    ├── /connect    → 连线（已有，含验证）
                    ├── /disconnect → 断线（v0.7 新增，GameThread 安全）
                    ├── /delete_node→ 删节点（v0.7 新增，含引用检查）
                    └── /command    → 白名单命令（set/get_pin_default_value 等；/execute 已废弃）
```

**工作流：**

```
你：修改 /Game/BP_Enemy 的 BeginPlay → PrintString 文本改为 "Enemy Spawned"
      │
      ▼
dev ──→ blueprint_editor.py：
          Step 1: freeze — 快照当前状态（/read）
          Step 2: execute — 逐个执行操作
            ├── update_node_param("PrintString", "InString", "Enemy Spawned")
            ├── disconnect_pin("OldEvent", "then")
            └── connect_pins_by_id("BeginPlay", "then", "NewEvent", "execute")
          Step 3: verify — 每个操作后验证生效
          Step 4: commit — 全部成功 → 记录事务日志
                   rollback — 任何失败 → 恢复到 freeze 快照
      │
      ▼
maintainer ──→ 汇报操作结果 + 事务日志路径
```

**安全机制：**

| 机制 | 说明 |
|:---|:---|
| **快照回滚** | 每操作前自动 freeze 状态，失败可恢复 |
| **P01 防假阳性** | 连线后双向 LinkedTo 验证（pitfalls #1） |
| **P02 防静默失败** | 前置参数校验 + 后置断言（pitfalls #2） |
| **跨图同属校验** | BreakLink 必须校验两 Pin 在同一图（KB 命中） |
| **GameThread 安全** | 所有 UE API 调用通过 `/disconnect` `/delete_node` 专有端点在 GameThread 执行 |
| **事务日志** | 每次操作写入 `logs/blueprint_editor.jsonl`，可审计 |

**Python 调用示例：**

```python
from blueprint_editor import BlueprintEditor

editor = BlueprintEditor()

# 1. 修改参数
result = editor.update_node_param(
    "/Game/BP_Enemy", "PrintString", "InString", "Enemy Spawned!"
)
print(result.details)  # ✅ PrintString.InString = Enemy Spawned! (已验证)

# 2. 断开旧连线
editor.disconnect_pin("/Game/BP_Enemy", "OldEvent", "then")

# 3. 建立新连线
editor.connect_pins_by_id(
    "/Game/BP_Enemy", "BeginPlay", "then", "NewEvent", "execute"
)

# 4. 删除废弃节点（可先干跑）
editor.delete_node("/Game/BP_Enemy", "DeprecatedNode", dry_run=True)
# → 🔍 干跑: DeprecatedNode — 4 引脚 (2 已连线)，可安全删除
editor.delete_node("/Game/BP_Enemy", "DeprecatedNode")
# → ✅ 已删除 DeprecatedNode（断开 2 条连线）
```

**CLI 快速调用：**

```bash
python blueprint_editor.py update /Game/BP_Test PrintString InString "Hello"
python blueprint_editor.py connect /Game/BP_Test BeginPlay then PrintString execute
python blueprint_editor.py disconnect /Game/BP_Test PrintString InString
python blueprint_editor.py delete /Game/BP_Test OldNode --dry-run
python blueprint_editor.py delete /Game/BP_Test OldNode
python blueprint_editor.py health
```

**输出物：**
- 操作结果直接返回（JSON 格式，含成功/失败/验证信息）
- 事务日志：`logs/blueprint_editor.jsonl`（每次操作一行 JSONL）
- 不生成 Markdown 报告（增量修改是即时操作，不是分析报告）

**测试状态（v0.7 FIX）：**
- ✅ 离线 Mock 测试：17/17 通过（`tests/test_blueprint_editor_mock.py`）
- ⏳ 端到端测试：待 UE 5.1 在线时执行（`tests/test_blueprint_editor_e2e.py`，pytest skip）

---

## 四、输出物说明

### 4.1 报告文件清单

| 模式 | 输出文件 | 包含内容 |
|:---|:---|:---|
| 🔍 分析模式 | `📊 {safe_name}_analysis_{timestamp}.md` | 7 章：基本信息卡片 → 变量清单 → 节点拓扑 → Mermaid 流程图 → 引用资产 → 关联蓝图 → 优化建议（与 blueprint_reader.py:1069 实际命名一致） |
| 🏗️ 创建模式 | `🏗️ {蓝图名}_创建方案.md` | 方案概述 → Step-by-step 分解 → 预估产出 → 待确认项 |
| 🩺 诊断模式 | `🩺 诊断报告_{关联蓝图}.md` | 日志摘要 → 根因分析 → 修复方案（含优先级） → 操作步骤 |
| 📐 优化模式 | `📐 {项目名}_架构报告.md` | 整体结构 → 架构指标 → 问题清单（按严重程度） → 优化路线图 |
| ✏️ 增量修改模式 | `logs/blueprint_editor.jsonl` | 事务日志（每行 JSONL：操作类型/蓝图路径/参数/结果/耗时） + 即时 CLI 输出 |

### 4.2 流程图标准

所有报告中的 Mermaid 流程图统一使用 **emoji+中文标注标准**，详见 `templates/mermaid_tags.md` v1.0。

> 快速查阅标签：`🚀入口` `🔀分支` `🔄循环` `🏃移动` `🎯转换` `📦变量` `⚡性能热点` `❓未识别`

**mermaid l2_online 测试（开发端验证命令 · 需真实 UE 环境 + 源码仓库）：**

`run_tests.bat`（开发端测试启动器，**不随发布包**）`online` / `e2e` 模式**不含** mermaid 在线用例（`test_6_l2_online` 标记 `ue_online`）。开发侧需要验证 L2 在线渲染（`allow_external=True` → mermaid.ink）时，先 `cd` 到本 skill 根目录（`ue5-automation/`），再单独跑：

```cmd
cd <skill 源码根目录>  :: 开发端源码仓库内的 ue5-automation 根目录（发布包内无 tests\，本命令仅开发端可跑）
python -m pytest tests\test_mermaid_render_audit.py::test_6_l2_online -v --ue-online --timeout=90
```

> ⚠️ L2 在线渲染会向第三方 `mermaid.ink` 发送 Mermaid 代码，含项目敏感信息时慎用（见 §2.4 Mermaid 渲染安全）；默认 L1 (mmdc) 本地渲染。

### 4.3 报告语言约定

- **中文为主**，技术术语保留英文（如 CastTo、BeginPlay）
- 节点标签：`{emoji} {中文动词+对象}`，如 `⚔️ ApplyDamage 25`
- 问题分级：🔴阻断 → 🟡警告 → 🟢建议
- 不确定处标注 `⚠️ 待确认`，不强行下结论

---

## 五、边界与限制

### 5.1 版本能力矩阵

| 功能 | v0.1 | v0.2（SKILL-06 就绪） | v0.3（完整体） | v0.4（混合通道） | v0.7（增量修改） | v0.9（CPP-APIS） |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|
| 读取蓝图基本信息+变量 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 读取蓝图节点+连线 | ❌ | ✅ 59 API | ✅ | ✅ | ✅ | ✅ |
| 创建蓝图骨架（资产+变量+组件） | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 自动连线节点 | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 创建自定义事件/函数图 | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 蓝图续写（在已有蓝图中添加节点） | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |
| **修改节点参数（SetPinDefaultValue）** | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| **按标识连线（connect_pins_by_id）** | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| **断开连线（disconnect_pin / BreakPins）** | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| **删除节点（delete_node / RemoveNode）** | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| **事务快照 + 回滚** | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| **组件添加+属性设置（AddComponent→SetComponentProperty）** | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
| **程序化读取变量（ReadVariables · 假阳性消除）** | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
| 诊断错误模式 | 15+ | 40+ | 40+ | 40+ | 40+ | 40+ |
| 架构统计+热力图 | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 循环依赖检测 | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |
| 继承深度 / 接口分析 | ❌ | ❌ | ✅ | ✅ | ✅ | ✅ |
| 混合通道（读写分离） | — | — | — | ✅ | ✅ | ✅ |

> 注：当前 SKILL.md 版本为 v0.14；v0.10 / v0.11 未新增 Bridge API，能力面与 v0.9 列一致。表中「诊断错误模式」列的 `15+` / `40+` 为**早期预估口径**——截至 v0.13，`config\error_patterns.json` **实装 19 条**（PTN-001~019 · 与 `docs\pitfalls.md` P01–P19 同源），实际可用模式数以实装为准。

### 5.2 已知限制

| 限制 | 影响 | 缓解方式 |
|:---|:---|:---|
| 蓝图节点读取依赖 C++ Bridge | v0.1 无法生成节点拓扑和连线 | ✅ v0.2 已解决（Bridge API 覆盖 · 随包 DLL 59 UFUNCTION） |
| 蓝图创建不支持节点连线 | 创建后需手动连线 | ✅ v0.2 已解决（ConnectPins API） |
| 大型项目扫描可能超时 | 200+ 蓝图项目扫描 > 5 分钟 | 增量扫描（仅扫变更文件）；后台异步+进度推送 |
| 截图识别准确率非 100% | 复杂节点图可能误识别 | 输出标注识别置信度；低置信节点标记 `❓` |
| 模式 E 仅支持已有蓝图的节点级增量修改 | 不做整图重建、不能替代模式 B 从零创建 | 整图重建 / 新建蓝图 → 模式 B（§3.2 创建模式）；结构分析 → 模式 A |
| 模式 E 不支持「从已有蓝图复制节点逻辑」 | 无法把 A 蓝图节点组整体复制到 B 蓝图 | Bridge API（59 项）无复制/克隆接口（§2.3 / §5.1 能力矩阵无此能力）；需手动重建或改走模式 B |

### 5.3 不支持的场景

- ❌ UE4.x 项目（API 差异较大）
- ❌ 非 UE 引擎项目
- ❌ C++ 源码级操作（仅蓝图层面）
- ❌ 运行时/已打包游戏的操作（仅编辑器环境）

---

## 六、FAQ

### Q1：为什么创建蓝图后节点没有连线？
> **A：** v0.2 已通过 C++ Bridge（Bridge API）支持自动连线。`ConnectPins` API 可以连接任意两个引脚。如果连线失败，请确认 Bridge 插件已正确安装并加载。

### Q2：分析模式为什么看不到节点详情？
> **A：** v0.2 已解决。现在通过 C++ Bridge（`ListNodes` / `GetPinInfos` / `GetNodeConnections` 等 59 API）可直接读取节点、引脚、连线拓扑，不再需要截图识别降级方案。

### Q3：端口 8889 被占用了怎么办？
> **A：** HTTP bridge 默认监听 `127.0.0.1:8889`，仅用于写操作（build/connect）。如果端口被占用：
> 1. 在 UE Python Console 中启动时指定其他端口：`ue5_bridge.start(port=8890)`
> 2. Agent 侧调用时传 `--bridge-port 8890` 参数
> 3. 或者通过环境变量 `UE5_BRIDGE_PORT=8890` 设置
> 
> 读操作（health/read）走 ue_send，不受端口影响。

### Q4：支持 UE5.3 / 5.4 吗？
> **A：** 随包 DLL 只实测 UE 5.1.x，**5.2–5.5 需自行重编译 bridge**（架构可行、未逐版本实测），5.6+ 未验证。不要假设高版本直接可用 —— 按 `UE_VERSION_GUIDE.md` 路径 B/C 操作，或先跑 `python scripts\check_ue_version.py` 自检。遇到不兼容问题请贴报错日志进入诊断模式。

### Q5：分析一个蓝图要多久？
> **A：** L1 级（基本信息+变量）约 5-10 秒。L2/L3 级（节点+连线）视蓝图复杂度而定，简单蓝图 < 30 秒，200+ 节点的大蓝图可能需要 1-2 分钟。

### Q6：创建蓝图时出错了能回滚吗？
> **A：** 创建模式在执行前会出方案让你确认，确认后才会动手。如果执行中出错（如编译失败），**创建的资产不会被自动删除**（避免误删已有资产），会提示你手动处理并存留诊断报告。

### Q7：诊断模式会自动修改我的蓝图吗？
> **A：** 默认 `auto_fix=true` 会自动修复可安全修复的问题（如引用替换、接口补全）。如果你希望**只看诊断不动手**，请在触发时说明"只诊断不修复"或设置 `auto_fix=false`。

### Q8：我能自己照着流程手动搭建吗？
> **A：** 可以。创建模式的方案输出就是一份完整的操作清单，包含每个 Step 的具体操作（在哪右键 → 选什么菜单 → 填什么参数），你可以照着在 UE 编辑器中手动完成。

---

## 七、故障排查

### 7.1 通信失败（HTTP 超时 / Connection Refused）

| 排查步骤 | 操作 |
|:---|:---|
| 1 | 确认 UE5 编辑器已打开，Bridge 已在 Python Console 中启动（`import ue5_bridge; ue5_bridge.start()`） |
| 2 | 确认 Remote Execution 已开启：`Config/DefaultEngine.ini` → `[PythonScriptPlugin]` → `bRemoteExecution=true`（面板：Edit → Project Settings → Plugins → Python → ☑ Enable Remote Execution） |
| 3 | 自检（/health 需鉴权，浏览器裸访问只会 403，不代表 bridge 挂了）：`python scripts\ue5_runner.py health`；或 `curl -H "X-Skill-Token: <token>" http://127.0.0.1:8889/health`（token 见 §2.4） |
| 4 | 若无法访问 → 检查 fastapi/uvicorn 是否已安装到 UE5 的 Python 环境（见 DEPLOY.md §七 步骤 8） |
| 5 | 若端口已被占用 → `netstat -ano | findstr 8889` 查看占用进程 |

### 7.2 Python 插件不可用

| 排查步骤 | 操作 |
|:---|:---|
| 1 | UE → Edit → Plugins → 搜索 "Python" → 确认 `Python Editor Script Plugin` 已勾选 |
| 2 | 若未勾选 → 勾选后重启编辑器 |
| 3 | 若仍然不可用 → 检查 UE 安装目录 `Engine/Plugins/Experimental/PythonScriptPlugin` 是否存在 |

### 7.3 蓝图编译失败（创建模式）

| 错误现象 | 可能原因 | 解决 |
|:---|:---|:---|
| "Parent class not found" | 指定的父类不存在 | 确认父类路径是否正确 |
| "Variable type not recognized" | 变量类型拼写错误或类型不存在 | 检查类型名（如 `float` 非 `Float`） |
| "Interface not implemented" | 蓝图未实现接口的所有函数 | 在蓝图编辑器中手动补全接口函数 |

### 7.4 报告质量不满意

> 如果报告未达到预期，请在反馈中说明：① 哪部分不够详细 ② 需要什么额外的维度。docs-writer会调整模板和示例库。

---

## 八、相关文档

| 文档 | 路径 | 说明 |
|:---|:---|:---|
| Mermaid 标签标准 | `templates/mermaid_tags.md` | 流程图 emoji+中文标注规范（24 标签+4 连线+5 色） |
| 分析报告模板 | `templates/analysis_report.md` | 7 章结构 + BP_EnemyController 示例 |
| 创建方案模板 | `templates/creation_plan.md` | 方案格式 + BP_PickupItem 示例 |
| 诊断报告模板 | `templates/diagnosis_report.md` | 诊断格式 + FInventoryItem 缺失示例 |
| 架构报告模板 | `templates/architecture_report.md` | 指标体系 + MyGame 项目示例 |
| 立项文档 | `./SKILL.md` | 完整技术架构、Agent 分工、模式用法（见本文件「一、技能概述」「二、前置依赖」「三、四种模式入口」等章节） |
| **增量修改引擎** | `scripts/blueprint_editor.py` | v0.7 新增：4 核心 API + freeze/execute/verify + 事务日志 |
| **Bridge 端点参考** | `scripts/ue5_bridge.py` | HTTP 端点清单（含 v0.7 新增 /disconnect /delete_node） |
| **已知陷阱清单** | `docs/pitfalls.md` | **19 个已知坑（P01–P19）**（ConnectPins 假阳性 / CDO 时序 / 线程安全 / **事件存在性误判漏建** / **同名节点定位** / **`/build` spec 字段名契约不一致** 等） |
| **质量门禁** | `docs/quality_gates.md` | 提交前质量门槛（⚠️ **仅源端 skill 目录，不随发布包分发**） |
| **预检清单** | `docs/preflight.md` | 运行前预检项（随包分发） |
| **v0.7 设计文档** | `output/UE-05-v07-blueprint_editor.md` | 增量修改模式的技术设计 + 测试计划（⚠️ **仅源端**，`output/` 不随发布包分发） |

---

> **手册版本 v2.0** | 角色分工疑问请对照 §1.3 或 init.py 生成的 role_assignments.yaml

## 附录 B：常见错误码

| 状态码 | 含义 | 最常见原因 | 解决 |
|:---|:---|:---|:---|
| **403** | Forbidden | 请求未带 `X-Skill-Token` 头，或令牌不匹配 | 读取 `%APPDATA%\ue5_bridge\token`（bridge 首启自动生成的随机 token），或两端统一设置 `UE5_BRIDGE_TOKEN`；**没有 test123 默认令牌**（见 §2.4） |
| **400** | Bad Request | 命令不在白名单 / 参数格式错误 | 检查命令拼写，参考「三、四种模式入口」各模式请求格式 |
| **500** | Internal Server Error | UE Python API 调用异常（如蓝图不存在、CDO 不可访问） | 查看 UE5 Python Console 输出的完整 traceback |
| **timeout** | 请求无响应 | UE5 编辑器未打开 / Bridge 未启动 / 端口被占用 | 走 §7.1 通信故障排查清单 |
