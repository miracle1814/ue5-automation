# 版本历史（从 SKILL.md 迁出 · v4.6 分层）

保留所有历史版本记录与更新日志原文，便于追溯。当前版本信息见 SKILL.md 顶部。

## v1.1（工具瘦身 + 分级）
## v1.0（定稿版）
## v0.27（人话表达 + 运行状况修正）
## v0.26（交付物清单 + 排版审计）
## v0.25（用户视角全链路 v4）
## v0.24（T-20261025-VERIFY · 双引擎真机验证与发布收敛）
## v0.23（T-20261024-UE5BRIDGE-SCREENSHOT · 截图反馈闭环：MCP 工具 `ue5_screenshot`（工具数 26→27，白名单 75→78，capture_viewport / capture_pie / project_actors_to_screen 三命令）——修改 → 截图 → 视觉自检 → 修正；base64 JPEG 进上下文（长边默认 768，UEBox 压缩阶梯）+ camera_used 机位溯源 + Pillow 可选 actor 标注（缺失跳过并告警，绝不失败）；落盘 <项目>/Saved/Screenshots/ue5-automation/（仪表盘画面面板同源）；⚠️ 截图命令需 v3.7 版桥 DLL —— 本版桥侧为**离线草稿**，回家 `build_for_engine.bat` 编译后可用，旧 DLL 会得到明确的重新编译提示（绝不假成功）；§3.8 视觉自检 SOP；v0.22 为整图声明式写入）
## v0.22（T-20261024-UE5BRIDGE-GRAPH-APPLY · 整图声明式写入（UEBox 透析主路径）：新命令 `search_blueprint_nodes`（只读查函数/事件/流程控制/变量候选 + 完整引脚签名）+ `apply_graph`（GraphSpec 一次事务建图连线设值：409/非空双守卫 → 事务外编译 → 读回验证 verified → 失败回滚，确定性拓扑序网格布局）；MCP 工具 `ue5_search_blueprint_nodes` / `ue5_apply_blueprint_graph`（工具数 24→26，白名单 73→75，描述交叉引用）；§3.7 写图 SOP（"Never build a graph one node at a time"，逐节点工具仅手术式微调）；v0.21 为编译报错自动修复闭环（诊断侧））
## v0.17（T-20260918-V301-READBACK · 回读置信度补强：UMG/材质设值回读比对 + FSlateColor 自适应；v0.16 为融合升级）
## v0.16（T-20260918-V3-FUSION · 融合升级：MCP 协议接入 + PIE 验证族 + 材质/UMG 新域 + 源码随包；v0.15 为独立验证轮）
## v0.15（T-20260918-VERIFY-FIX · 独立验证轮：缺陷族 E-1~E-10 修复 + 全库口径清理，见下方 v0.15 日志；v0.14 历史内容保留其后）
## v0.14（T-20260917-SKILL-CLEANUP-V219 · 蓝图缺陷 **D-5~D-8** 修复 + 文档/元数据口径清理：`/build` 的 `events[]` / `functions[]` **字段名契约对齐**（照文档写不再得空名）· `add_function_node` **回传 node GUID** · `delete_asset` **保存作用域收窄**（不再静默提交未保存关卡））  
**手册版本：** v2.4  
**更新日期：** 2026-10-24（v0.23）  
**适用 UE 版本：** **5.1.x 开箱即用**；5.2–5.5 需自行重编译 bridge；5.6+ 未验证 —— **升级路径与出口见 [`UE_VERSION_GUIDE.md`](../UE_VERSION_GUIDE.md)**（随包）
>
**📦 随包范围（第三方使用者必读）：** 发布包只含**运行时脚本**（`scripts/` / `templates/` / 文档）。`tests/`、`conftest.py`、`run_tests.bat` 属**开发端测试基建**，**默认不随发布包**（仅 `publish.py --with-tests` 时携带）——下文历史更新日志中出现的 `run_tests.bat` 命令均为**开发端**用法，第三方发布包内**无此文件**，请勿尝试执行。
>
### v0.18 更新日志（T-20260918-V32-UE58 · 5.8 桥编译实证 + extras 端到端验收）
- ✅ **UE 5.8 侧全部打通（实测）**：C++ 桥按 5.8 重编译成功（引擎 **5.8.2-56702186** · 工具链 **MSVC 14.44.35207**）并完成端到端验收 ——
  `5.8 编辑器 → 官方 MCP(127.0.0.1:8000) → UE5AutomationExtras 工具集 → C++ 桥（**88 个方法**，含全部 UMG API）`：
  `ue5_status` 回读 bridge_loaded=true / method_count=88 / has_umg=true；`ue5_pie_state`、`ue5_actor_find`（无参枚举 50 actor）、`ue5_logs` 实调通过；错误路径 fail-loud 可读。
- 📦 **预编译 5.8 DLL 随包**：`bridge/prebuilt-5.8/`（DLL + **UnrealEditor.modules**）→ 5.8 用户**零编译**即可获得节点级桥能力（复制两文件到 `<Project>/Plugins/BlueprintPythonBridge/Binaries/Win64/`）。
- 🔧 **5.8 兼容修复（源码级）**：`FPackageName::DoesPackageExist` 的 Guid 重载在 5.8 移除 + `UPackage::SavePackage` 老参列重载移除 → 统一改现代签名（5.1/5.8 同一套代码）→ 单源码跨版本。
- 🩹 **`build_for_engine.bat` 修复**：现在**同时同步 `UnrealEditor.modules`**（UE 模块清单含 BuildId，缺失会以 `Incompatible or missing module` 拒载——5.8 冒烟实测踩中并修复）。
- 🧩 **extras 工具集两处实修（5.8 实测）**：① 工具方法返回 `dict` 会被注册器拒绝（`missing specification for contained type`）→ 统一 `-> str` + JSON 字符串；② 空字符串默认值会被 schema 生成器视为**必填** → 可选字符串参数改 `"*"` 哨兵默认（空串/星号均表示"不过滤"）。
- 📘 **5.8 官方 MCP 开启方式（实测必需）**：MCP HTTP 服务**默认不自动启动**——项目 `Config/DefaultEditorPerProjectUserSettings.ini` 写：
  `[/Script/ModelContextProtocolEngine.ModelContextProtocolSettings]` + `ServerUrlPath=/mcp` + `ServerPortNumber=8000` + `bAutoStartServer=True`，重启编辑器后监听 127.0.0.1:8000。
- ⚠️ **工具链要求（实测排定）**：5.8 编译需 **MSVC ≥14.44**（14.40–14.43 被 UBT 封禁；14.38 撞引擎 C7539）且 VS2022 产品本体需 ≥17.14。

### v0.17 更新日志（T-20260918-V301-READBACK · 回读置信度补强 + 5.8 工具链结论）
- 🎯 **取舍#6 落地：写操作回读比对覆盖补满**（"绝不假成功"的最后一环）：
  - **UMG 设属性**：C++ 新增 `ReadWidgetProperty`（导出属性文本；失败透传 `ERROR:` 前缀）→ `widget_set_property` 设置后**强制回读比对**，不一致即 fail-loud（返回体新增 `readback`/`verified`/`applied_value`）。
  - **材质设属性**：Python 侧回读比对（此前只回传不回判）→ 不一致即 fail-loud（新增 `verified: true`）。
  - 新增共享助手 `_value_matches()`：精确 → 数值容差（f-string 浮点显示差）→ 去引号 → **数字 token 序列比对**（结构体导出格式差）→ 覆盖三类真实差异。
- 🔴 **回读机制首战抓到真实假成功（并已修复）**：`ColorAndOpacity` 是 **FSlateColor**，用美术习惯的 `(R=..,G=..,B=..,A=..)` 格式会被 ImportText **静默接受但值不变**（回读恒为白色）——v3.0 报告中该用例的"成功"实为**假阳性**，特此更正。修法 = **自适应格式重试**：回读发现回读文本含 `SpecifiedColor=` 且请求未包装时，自动以 `(SpecifiedColor=(...))` 重试并回读确认（返回体 `note` 透明记录）。实测：请求 (0.1,0.2,0.3,1.0) → 应用包装格式 → 回读 (0.100000,0.200000,0.300000,1.000000) → verified=true。
- 🔧 **5.8 桥编译工具链结论（实测排定）**：UE 5.8 要求 **MSVC ≥ 14.44**（`static_assert(_MSC_VER >= 1938)` 是下限声明，但 **14.38 实测撞引擎 `ContainerAllocationPolicies.h` C7539 严格一致性错误**；14.40–14.43 被 5.8 UBT 明令封禁）→ 可行区间 = **14.44+（建议 14.50.35717）**。安装：VS Installer `modify --add Microsoft.VisualStudio.Component.VC.14.44.17.14.x86.x64`（需 UAC 提升）。
- 📊 回归：**502 passed / 95 skipped / 0 failed**（+5 为 `_value_matches` 单测）。
- 包内 DLL（v3.9 双引擎重编译）：**5.1 = 1,048,576 B · MD5 1f0ae311905036b9b021599954d53814**；**5.8 = 901,632 B · MD5 9d5908c7baa3293f3e3a9b4dbd48cfb1**（含截图闭环/投影修复/GT 直呼；双引擎均真机验证）。

### v0.16 更新日志（T-20260918-V3-FUSION · 融合升级：MCP 协议接入 + 编辑器广度能力 + 材质/UMG 新域）
- 🚀 **MCP 协议接入层（`/mcp` 端点 · 零依赖手写）**：官方 mcp SDK 需 Python ≥3.10，而 UE5.1 内嵌 Python 为 3.9 → 手写最小 MCP（JSON-RPC 2.0 / Streamable HTTP：initialize 版本协商 · tools/list · tools/call · ping · notifications 202 · resources/prompts 空列表）。**23 个 MCP 工具**（诊断/读写/构建/PIE/日志/批量 + `ue5_execute_command` 白名单透传，描述含全量命令名）；鉴权新增 `Authorization: Bearer` 与 `?token=` 双兼容通道（原 X-Skill-Token 不变）。任何 MCP 客户端（ZCode/Claude 等）可直连 **UE 5.1–5.5** 编辑器 —— 接入文档见 `docs/MCP-ACCESS.md`。
- 🎯 **编辑器广度能力（移植自官方 MCP 验证范式）**：
  - **PIE 运行时验证族**：`pie_start(mode=simulate|pie)` · `pie_stop` · `pie_state` —— 跨库候选链实测修正（UE5.1：`editor_play_simulate` 在 EditorLevelLibrary，`editor_request_end_play`/`is_in_play_in_editor` 在 LevelEditorSubsystem，`editor_end_play` 才是 ELL 的停止名——旧单库探测全 missing）；**实测：PIE 期间 HTTP bridge 存活**（运行时读属性/传送全通）。
  - **Actor 基础族**：`find_actors` · `spawn_actor`（新增 `mobility` 参数）· `set_actor_transform` · `get_actor_properties`（world=editor|pie）。
  - **日志查询**：`logs_read`（游标增量 + category/pattern 过滤）—— 诊断模式从"用户贴日志"升级为"Agent 自取日志"。
  - **批量执行**：`editor_batch`（白名单命令序列单次请求，fail-loud 可中止）。
- 🔴 **实测修复：PIE 传送假成功**（"返回值说谎"族）：`set_actor_location(vec, False, False)` 在 PIE 中对 Static 组件**静默无效**但命令报 success=true → 改为 teleport=True 优先 + **设置后回读比对，未生效即 fail-loud**（并回传 mobility 供排查）。实测：Movable Actor 传送 verified=true + 独立复读一致。
- 🎨 **材质族（9 条命令 · 1 个新 C++ API）**：探针实测 UE5.1 `MaterialEditingLibrary` 完整暴露（72 方法）→ `material_create` / `material_add_expression`（**v0.20：回读确认表达式落图**）/ `material_connect_expressions`（**v0.20：回读目标引脚实际连到的表达式并比对，另检测引擎插入的隐式转换节点**）/ `material_connect_property`（**v0.20：回读主属性实际连到的表达式**，base_color 等 14 种属性映射）/ `material_set_expression_property`（类型自适应 + 回读回传）/ `material_compile`（**v0.20：`None` 不再漏判**）/ `material_read` / `material_instance_create` / `material_instance_set_parameter`（scalar/vector/texture）。三条连线/建表达式命令的返回体带 `verified` / `warnings`——`verified` **只表示"回读比对执行且通过"**，不因非致命告警（如隐式转换节点）变 false；比对读不到时 `verified=false` + 告警，命令不阻断。
- 🧩 **UMG 控件蓝图（4 条命令 + 4 个新 C++ API）**：`UWidgetTree` 未向 Python 暴露（与 UEdGraph 同坑）→ C++ 桥新增 `CreateWidgetBlueprint`（父类 UUserWidget + 自动建树编译）/ `AddWidgetToTree`（根或面板子级，重名 fail-loud）/ `SetWidgetProperty`（FindFProperty + ImportText_Direct）/ `ReadWidgetTree`；命令层 `widget_create` / `widget_add_child` / `widget_set_property` / `widget_read_tree`（旧 DLL 上调用 → 可读缺件提示 + 重编译指引，绝不假成功）。WidgetBlueprint 的 EventGraph 复用既有节点级图能力。
- 📦 **白名单 50 → 72 条**（+PIE×3 + Actor×4 + logs + batch + 材质×9 + UMG×4）；`/command` docstring 同步。
- 🔧 **C++ 桥源码随包**：`bridge/BlueprintPythonBridge/Source/`（.cpp/.h/Build.cs 全量）+ `build_for_engine.bat <UE_ROOT>` 一键编译（自动备份旧 DLL + 部署）+ `BUILD.md`（5.2–5.8 自助重编译路径；含 Win32Exception 998 / VS 工具链 / API 变动三类问题的处理）→ UE_VERSION_GUIDE 路径 B 对第三方**真正可自助**。
- 🧩 **5.8 官方 MCP 融合插件（`bridge/ue58-extras/`）**：纯 Content 插件（照 ZCodeMCPExtras 已验证模式）注册 `UE5AutomationExtras` 工具集 —— 8 个工具（status/PIE 状态/日志/Actor 查改/编译 + 桥部署后的**节点级图回读与带拓扑验证的连线**，即官方 DSL "exec 线为空"缺陷的正解路径）。安装 3 步见其 README。
- 📊 **回归与实测**：离线套件 492 passed / 95 skipped / 0 failed（+38 v3.0 新用例：MCP 协议/鉴权/batch/logs/别名）；UE 5.1.1 活体：MCP 握手与工具调用、PIE 启动→状态→运行时对象→传送回读→停止、日志、批量全链路通过。
- ⚠️ **诚实边界**：UE 5.8 的 C++ 桥重编译被本机 MSVC 工具链限制阻断（UE 5.8 UBT 封禁 MSVC 14.40–14.43，本机为 14.42）→ 5.8 侧交付源码 + 一键编译脚本 + 纯 Python 工具集；安装 14.50+ 工具链后运行 `build_for_engine.bat` 即可补齐节点级能力。

### v0.15 更新日志（T-20260918-VERIFY-FIX · 独立验证与修复轮）
- 🔍 **本轮性质**：由独立验证方对 v2.19 发布包做全量审查（4 路静态审查 + 离线回归 + UE 5.1.1 活体实测），修复验证中发现的缺陷族 **E-1~E-10**（E = Evidence-based，区别于开发端自报的 D 族）。
- 🔴 **E-1（诊断引擎默认调用即崩）**（`log_parser.py`）：日志含**未匹配错误行**（真实 UE 日志最常见形态）时 drafts 落盘对绝对路径调用"只许相对路径"的 sanitizer → 未捕获 ValueError → 主报告已写、drafts 全丢、进程崩溃。修法 = sanitizer 增加 `allow_absolute`（相对路径的 cwd 包含校验与 `..`/`~` 拒绝语义不变）；同时 `--output` 支持绝对路径、缺文件退出码 0→2、无操作 auto-fix 桩不再谎报 "applied"。
- 🔴 **E-2（Windows 上 L1 图渲染必崩且不降级）**（`mermaid_render.py`）：npm 的 mmdc 是 .cmd 垫片，`subprocess` 裸名只按 .exe 解析 → FileNotFoundError 且异常类型不在捕获链 → 整批渲染崩溃。修法 = `shutil.which` 解析可执行文件 + OSError → MermaidRenderError（L2/L3 降级链恢复生效）；输出文件名加短随机后缀防同秒覆盖。
- 🔴 **E-3（/build 编译失败被谎报成功）**（`game_thread_build.py.tmpl`）：`compile_blueprint` 的 bool 返回值被忽略、无条件记 step ok → 编译失败时 `/build` 返回 success=true + compile_status=BS_UpToDate（正是本项目自订"返回值说谎"红线）。修法 = 按返回值定论，False → step error + success=false。
- 🔴 **E-4（disconnect/delete 假成功与不可达）**（`blueprint_editor.py`）：① `_get_pin_links` 只按标题匹配 + 读取失败也返回空 → GUID 入参/桥故障时谎报"本就无连接"且不执行断开（修法 = GUID 匹配 + 读取失败直调 /disconnect 权威定论；bridge 连线回传补 from_guid/to_guid）；② `_get_node_info` 硬编码 EventGraph → 函数图/宏/构造脚本节点永远"不存在"（修法 = 全图搜索）+ 读取失败与节点不存在可区分；③ 回滚承诺无实现（修法 = `rollback()` 实装：connect/disconnect/update_param 可逆，delete 明示不可逆）。
- 🟠 **E-5（analyze_live 直连路径瘫痪）**（`analyzer.py`）：BlueprintReader 构造签名错 + 不存在的 to_dict + self.result 未初始化三重坏 → UE 直连扫描完全不可用。修法 = 对齐 BlueprintReader 实际 API + 惰性初始化；循环依赖去重补旋转规范化（同一环不再重复上报）。
- 🟠 **E-6（5.2–5.5 引擎误部署 5.1 DLL 零拦截）**（`bridge/deploy.py` + `install_dependencies.bat`）：新增**引擎版本闸**（读 Build.version / .uproject EngineAssociation，非 5.1.x 默认拒绝 + `--force` 显式覆盖）、PermissionError 可读化、--target 校验 .uproject、HKCU 注册表探测；bat 补部署失败 errorlevel 检查（不再假成功）、管理员运行兼容、Python ≥3.10 版本闸、CRLF 规范化。
- 🟠 **E-7（读通道引擎发现退化）**（`ue_send.py`）：移除开发机硬编码路径 → 注册表（HKCU+HKLM / WOW64_64KEY）+ 各盘符 × UE 5.1~5.5 动态探测；`send(timeout)` 形参真正生效（发现阶段）；`--bridge-diag` 解析结构化结果并按 bridge_loaded 设退出码（0/4）。
- 🟡 **E-8~E-10（健壮性）**：`check_ue_version.py` 本机探测禁用代理（系统代理下 127.0.0.1 误报）；`preflight.py` 批量 manifest 数组形态 + 颜色 isatty 探测；`blueprint_builder.py` CLI spec 嵌套转换 + 失败退出码；`blueprint_reader.py` save_report 两处崩溃/覆盖；`blueprint_editor.py`/`analyzer.py` GBK 控制台 emoji 崩溃；`setup.py` 死代码 DLL 路径 + 8889 端口冲突。
- 📚 **文档口径清理**：token「默认 test123」错误口径三处更正（bridge 端随机 token + token 文件，无 test123）；/health 需鉴权（浏览器裸访问 403 非故障）；幽灵端点 /execute 两处更正为 /command；SKILL.md 白名单口径 48→50；UE「5.1 或更高」回潮表述按 UE_VERSION_GUIDE 收敛；手册版本统一 v2.0；各文档版本号对齐 v0.15。
- 📊 **回归**：离线基线 `435 passed / 92 skipped / 0 failed` 保持全绿（1 个固化了过时数字的测试断言同步修正为动态口径）；UE 5.1.1 活体实测见随包《v2.20 验证报告》。
- 🎯 本次修改范围：`scripts\` 11 文件 + `bridge\deploy.py` + 两个 install bat + `setup.py` + 全库文档；**⛔ 不改 bridge DLL、⛔ 不改 C++ 源码**。

### v0.14 更新日志（T-20260917-SKILL-CLEANUP-V219 · 蓝图缺陷 D-5~D-8 修复 + 文档/元数据口径清理）
- 🎯 **D-5（`/build` 的 `events[]` 字段名契约与实现不一致 · 照文档写会得「空事件名」）**（`scripts/ue5_bridge.py` + `scripts/game_thread_build.py.tmpl`）：`BuildRequest.events` 注释与 `ue5_runner.build()` docstring 均写 `{"name": …}`，但 bridge 原样透传、模板只认 `event_name` → 传 `name` 得**空事件名** → step `add_event:` + `add_event_node returned None` + `compile_status=BS_Error`（**四条报错提示全指错方向**）。
  - **修复**：新增 `_normalize_build_events()`（**L5353**）+ `/build` 接线（**L5595**）——主字段 `event_name`、兼容别名 `name`；冲突取主字段并回传 `warnings`；两者皆空 → **fail-loud**；`game_thread_build.py.tmpl` 同步**双层兼容**。
- 🔊 **D-6（`add_function_node` 不回传节点 GUID）**：返回体补回 **`node_guid`**（`_pywrap_recover_node_guid()` · `ue5_bridge.py` **L1733**）→ 建完节点**可直接**喂 `set_pin_default_value`（⛔ 不必再 `read_nodes` 回读全图按位置反查）。
- 🩹 **D-7（`delete_asset` 保存作用域过宽 → 静默提交未保存的关卡 / 资产）**：旧实现第②步调 `save_dirty_packages(True, True)` = 保存**全工程脏包** → 用户「删一个临时资产」会连带写 `ElevatorDemo.umap` 等无关资产；改法 = **仅保存「引用被删资产的包」**（`_referencers_options()` **L4200** / `_collect_related()` **L4221** / `_save_related()` **L4256**；旧 `_save_dirty()` 整体删除），返回体新增 **`save_scope`** / **`saved_related_packages`** / **`save_skipped_reason`** 三个可观测字段。
- 🎯 **D-8（`/build` 的 `functions[]` 字段名契约与实现不一致 · 与 D-5 完全同型）**：新增 `_normalize_build_functions()`（**L5425**）→ `/build` 接线 **L5603**、`/build-batch` 接线 **L7345**（两处 GT 透传 L5710 / L7414 均为**已归一化**结果）——主字段 `function_name` + 兼容别名 `name`、`function_path` **可由 `target_class` 推导**（`<target_class>:<函数名>`，并标注来源）、冲突取主字段 + `warnings`、**函数名与路径皆缺 → fail-loud**（点名 `functions[i]` + 回传**实收 keys**，且**早于任何副作用**：不建资产、不留半成品）；模板侧双层兼容（`game_thread_build.py.tmpl` L309-343）。
- 📚 **文档 / 元数据口径清理**：
  - `/command` 白名单口径 **48 → 50 条**（补登 `delete_asset` / `unpin_console_refs` 两行 —— 二者此前**从未登记进任何命令表**）→ `workflow.md` 附录 B / `API_REFERENCE.md` / `docs\L2-COMMANDS.md` / `docs\SOP-UE-HTTP-BRIDGE.md` **四处同步**。
  - `config\error_patterns.json` 错误模式 **15 → 19 条**（PTN-016~019 登记闭环；其中 P16 原为**空头登记**「关联 PTN-016 但库内无此条」，一并补齐）。
  - `docs\pitfalls.md` 新增 **P19**（`/build` spec 字段名契约不一致）+ 附录映射行 + 版本 v1.3；全库「陷阱 / 模式」计数口径同步（**18 → 19 个已知坑（P01–P19）**）。
- 📦 **发布包口径修正**：`scripts\publish.py` 桥接段清单黑名单口径与**打包剔除侧**对齐（manifest `bridge.files` 补回**故意随包**的 `DELIVERY.md`，消除「包里有、清单里没有」的账实不符）；`setup.py` / `install_dependencies.bat` **建立真源血统**（原为「保留包内现有」的无源孤儿产物 → 现随真源同步）。
- 📊 **回归**：离线基线 `405 passed / 92 skipped` → **`435 passed / 92 skipped / 0 failed`**（+30 全为 D-8 新增用例；`skipped` 与基线**逐数一致**）。
- 🎯 本次修改范围：`scripts\ue5_bridge.py`（363,739 → **372,793 B**）+ `scripts\game_thread_build.py.tmpl`（22,584 → **24,984 B**）+ `scripts\ue5_runner.py`（29,433 → **30,119 B**）+ `docs\SOP-PIPELINE.md`（16,245 → **17,292 B**）+ `scripts\README.md`（6,183 → **6,331 B**）+ `docs\pitfalls.md` + `workflow.md` + `API_REFERENCE.md` + `docs\L2-COMMANDS.md` + `docs\SOP-UE-HTTP-BRIDGE.md` + `SKILL.md` + `USER_GUIDE.md` + `config\error_patterns.json` + `scripts\publish.py`；**⛔ 不改 bridge DLL、⛔ 不改 C++ 源码**。
- ⚠️ **诚实边界**：本节所列**离线回归证据**来自开发端全量套件；**活体 UE 复测**（D-5/D-8 别名与 fail-loud · D-6 GUID 回传 · D-1/D-2 回归）由发布方独立执行，结论见随包交付报告。

### v0.13 更新日志（T-20260916-UE5AUTO-HALLBEACON-D1D4-FIX / T-20260917-RELEASE-V218 · 蓝图缺陷 D-1~D-4 修复随包）
- 🔴 **D-1（阻断级 · `/build` 事件存在性判定误判 → 静默漏建）**（`scripts/game_thread_build.py.tmpl`）：`/build` 建非自建事件（如 `ReceiveActorEndOverlap`）时返回 `success=true`、**节点却没建**；根因 = 事件候选标题表把**所有**事件的本地化标题无条件塞进候选，与目标事件**无语义绑定** → 图中脚手架自带的「事件Actor开始重叠」误命中 → 判 `exists` → 静默跳过创建。
  - **修复**：新增 `_norm_event_key()` 语义键（去 `event`/`receive` 前缀 + 去 `_`/空格 + 小写），**非本事件标题绝不入候选**；本地化映射表 9 → 15 键（补 `ActorEndOverlap` / `ReceiveTick` / `ReceiveDestroyed` 等缺口）；`exists` 步回传 `matched_by` / `matched_title` / `candidates`（**可审计**，不再"黑箱 exists"）。
  - **效果**：不再静默漏建、不再重复创建同名事件；映射表外的事件名退化为「候选=[原名] → 建不出即 fail-loud」。
- 🎯 **D-2（`set_pin_default_value` / `get_pin_default_value` 支持 GUID 定位）**（`scripts/ue5_bridge.py`）：`node_id` 现**同时接受节点标题与 32 位 GUID**（亦可用显式别名 `node_guid`）→ **同名节点场景可按 GUID 精确设值**（此前 3 个同名「设置Actor在游戏中隐藏」只能命中其一）；返回体新增 `matched_by` ∈ {`guid`,`title`}；**标题定位向后兼容**；**零 DLL 改动**（复用既有 pywrap `GetNodeByGuid` 通路）。
- 🔊 **D-3（`add_function_node` 建节点失败 fail-loud）**：DLL 返回 None 时**必抛 `BridgeError`** 并附可读原因 + **函数名候选**（原名 / 去 `K2_` / 加 `K2_`），不再返回 `{"added_node": …}` 的**假阳性成功**（实测：`/Script/Engine.Actor:K2_SetActorHiddenInGame` → 真实函数名为 `SetActorHiddenInGame`，`K2_` 前缀仅用于重载冲突函数）。
- 🩹 **D-4（`unpin_console_refs` 缺参裸 TypeError 修复 + 文档补充）**：白名单绑定改为默认 `""`，空参 / 无参返回**可读错误**（`success=false` + `error`），⛔ **不执行任何删除**（本命令**无全量清理语义**）；`API_REFERENCE.md` 同步补入参说明。
- 📊 **回归**：离线基线 `344 passed / 92 skipped` → **`361 passed / 92 skipped / 0 failed`**（+17 全为本单新增用例，零跳过变化）。
- 🎯 本次修改范围：`game_thread_build.py.tmpl`（16,739 → 20,766 B）+ `ue5_bridge.py`（337,977 → 346,945 B）+ `API_REFERENCE.md`（12,508 → 14,539 B）+ **本次文档同步**（SKILL.md / USER_GUIDE.md / workflow.md / `docs/pitfalls.md` / `docs/SOP-UE-HTTP-BRIDGE.md`）；**不改 bridge DLL、不改 C++ 源码、不改运行语义之外的行为**。
- ⚠️ **诚实边界**：D-1 / D-2 的**在线（活体 UE）复验尚未执行**——本版仅含**离线回归**证据；D-1 最坏表现为「建不出 → fail-loud」，**不再静默漏建**。
### v0.12 更新日志（T-20260915-UE5PKG-VERSION-DOC · UE 版本适配与升级出口）
- 📘 **新增 `UE_VERSION_GUIDE.md`（随包 · 根级）**：把"随包 DLL 只实测 5.1"从**一句免责声明**升级为**可执行的升级路径 + 出口**——
  - **三层适配结构**（L1 C++ bridge DLL 强绑定引擎 / L2 UE 内 Python 中层 / L3 宿主侧 Python 不绑定），明确"换版本只需重编译一个 ~148 KB 模块"；
  - **路径 A/B/C**：5.1 零改动 · 5.2–5.5 取源码重编译（含 `Build.cs` 模块依赖清单、VS2022 14.42 并行编译坑 `-NoParallelExecutor`）· 5.6+ 按编译错误分类排查；
  - **§8 出口与降级矩阵**：拿不到源码时**仍可用**的只读能力（L1 原生探针 / 项目评估 / 报错诊断 / 部署自检为纯 Python），写通道不可用一栏如实标注；
  - **§6 升级后验收 6 项**（含关键判据：`degrade_level` 必须为 `L2_BRIDGE`，否则 bridge 实际未生效）；
  - 附 5.1 基线数字（`method_count=80` / `missing_apis=3`，实测自 `/diag`）与事实来源表。
- 🧰 **新增 `scripts/check_ue_version.py`（随包 · 只读）**：注册表 + 常见目录 + `--engine` 定位引擎 → 读 `Engine\Build\Build.version` → 判定并给下一步；`--online` 追加 `/health` + `/diag` 探测；退出码 `0=5.1 可用 / 2=需重编译 / 3=未知或未检测到`，可直接用于脚本编排。
- ✏️ **文档口径修正**：SKILL.md / `USER_GUIDE.md` 原先的"适用 UE **5.1+**"表述**作废**（DLL 仅实测 5.1，`5.1+` 会造成"高版本也能直接用"的误导），统一改为「5.1.x 开箱即用；5.2–5.5 需重编译；5.6+ 未验证」并指向本指南。
- 🎯 本次修改范围：新增 2 文件（指南 + 自检脚本）+ `publish.py` 根白名单登记 + SKILL.md / USER_GUIDE.md / DEPLOY.md 三处口径与指针同步；**不改任何运行时代码逻辑**。
>
### v0.11 更新日志（T-20260909-SKILL-GEN-C-INIT-CONVERGE-REMAIN · 语义收敛定稿）
- 🔴 **init.py 语义收敛（v1.0「编排器」→ v1.1「薄执行器」）**：v0.10 日志中「一键初始化/宿主适配**编排器**」表述**作废**。实际实现为**薄执行器**——只做探测 + 生成 role_assignments.yaml **建议稿（status: proposed）**，**不自动生效**
  - **自动语义分配 = 预加载建议稿**（role_source: auto），未经人工确认 `status: proposed`，**不作为生效配置**
  - **人工确认门**：`--assign <id>=<role>` 显式指派核心角色，或手编 role_assignments.yaml 后 `--reconcile --apply`，将角色升级为 manual → `status: confirmed` 才生效
  - **single_agent 确定性指派**：≤1 enabled agent 时无选择空间 → 宿主 `all_in_one`（role_source: deterministic）→ `status: confirmed`（无需人工通道）
  - **防静默覆盖 GATE**：既有 confirmed 配置 + 无人工确认通道 + role_map 变化 → 拒绝（exit 3），禁止自动建议稿覆盖生效配置
- 🧪 **发布门禁 fixture**：新增 `tests/test_init_single_agent.py` 4 用例（single_agent 确定性指派路径：detect mode → all_in_one deterministic → decide confirmed 全链实测通过，0.19s）
- 📋 **§2.5 部署清单语义同步**：改为「薄执行器 + 人工确认门」引导（见下）；DEPLOY.md「新机器初始化」章节 / API_REFERENCE.md init.py CLI/产物契约条目本次一并补全（兑现 v0.10 日志声明）
- 🎯 本次修改范围：仅 SKILL.md / DEPLOY.md / API_REFERENCE.md 三文档 + tests fixture；不重写 init.py（62,601B / 1504 行已 py_compile 通过）
>
### v0.10 更新日志（T-20260909-SKILL-GEN-C-INIT · 通用化初始化入口）
- 🚀 **新增 `scripts/init.py` 一键初始化/宿主适配编排器（v1.0，随包分发）**：phase0-5（参数解析 → 能力探测三级链 → UE 环境探测 → 一键配置 → health 自检 → 产物），解决 skill 跨机器/跨宿主机首次部署时的手工适配问题
  - **能力探测三级链（phase1）**：L1 `list_agents API`（localhost:8088 `/api/agents`）→ L2 逐 agent 读 `agent.json`（tools 硬能力）→ L3 读 `system_prompt_files`（AGENTS/SOUL/PROFILE 语义标签）
  - **UE 能力判定（4 硬项 + 软判据）**：enabled + shell + file_io + python 全过，且身份文本（description/name）命中 UE 词表 → `capable(high)`；system_prompt 命中仅增强不翻转（防团队 prompt 全判 capable 误判）
  - **角色分配（role_assignments.yaml schema v1.0）**：orchestrator / ue_executor(+backup 两级) / screenshot_ocr / report_writer / archivist；单 Agent 环境自动 all_in_one；`--assign <id>=<role>` 手动覆盖（不抢自动 backup）、`--reconcile` 读回手编角色合并
  - **默认 dry-run 只读**，`--apply` 才落盘 `<skill>/output/`（publish 黑名单，防漂移入包）；不删除既有文件；不创建任何自启/定时/常驻服务
  - 离线自测：py_compile + 真实 11 agents dry-run（探测链/deploy 定位/UE 引擎 `（本机开发路径）` 全命中）+ 4 场景（默认/手动覆盖/backup/非法角色）+ apply 回读 + reconcile 手编保留
- **📋 §2.5 部署清单重构**：改为 init.py 引导式（先探测 → 再按缺项部署），附人工兜底步骤
- **📄 DEPLOY.md** 新增「新机器初始化」章节（二·五）；API_REFERENCE.md 新增 init.py CLI/产物契约条目

### v0.9 更新日志（T-20260908-CPPAPIS-CLOSEOUT · CPP-APIS 在线闭环收尾）
- ✅ **T-20260823-UE5BRIDGE-CPP-APIS 在线闭环（2026-09-08 实测）**：SetComponentProperty + ReadVariables C++ API（8/23 离线 17 项通过）经 DLL 部署（437,248B · 旧版备份 .bak5）后在真实 UE（dev-project / 5.1.1 · Bridge method_count 48）验证通过
  - **CPP-APIS 在线实测证据**：`read_variables` C++ 路径返回真实变量清单（BP_HelloMirror DefaultSceneRoot + 临时蓝图 **Health/MaxHealth**，消除 Python new_variables 假阳性/降级）；`add_component` properties → **SetComponentProperty** 设置 BoxExtent=[200,100,50] → spawn 实测 `CPP_Box Extent = {x:200, y:100, z:50}` **精确生效**（未 fail-loud）
  - **UE 在线功能测试**：离线基线 133 passed · 5 skipped · 0 failed 零回归（53.33s）；e2e 38 passed 全绿（含真实 UE 在线 7 项 = blueprint_e2e 4 + analyzer_phase2 3）；mermaid `--ue-online` 9 passed（l2_online 解锁）→ 原 5 skipped 全解锁
  - 验证固化：`docs/VERIFICATION-PHASE2.md` §七（2026-09-08 UE 在线功能测试）
- **🔑 token 403 根因修复（2026-09-08）**：offline 测试模块 import 时 `setenv UE5_BRIDGE_TOKEN` 污染 pytest 进程 env → e2e `BlueprintEditor()` 优先读 env 拿到错误 token → 真实 UE Bridge HTTP 403（8/23 挂起 403 的真根因）→ e2e fixture 改为**显式直读 `%APPDATA%/ue5_bridge/token`**（`_resolve_real_bridge_token()`，免疫 env 污染；优先级：token 文件直读 > 硬编码 header > 依赖 env）
- **📋 C++ API 清单同步 v0.9**：§2.3 从「21 API」修正为 **27 API**（补 v0.8-v0.10 新增 6 个：AddComponentToBlueprint / SetComponentProperty / ReadVariables / BreakPins / RemoveNode / ExecutePythonOnGameThread；DLL 163KB → 437KB；能力矩阵补 v0.9 列 + 「组件添加+属性设置」「程序化读取变量」2 行）
- **📄 裁决语义补录**：C++ bool + `FString& OutError` 反射规则（**''=成功 / None=失败无文本 / 非空 str=失败+错误文本**）——`add_component` / `set_component_property` 实测对齐
- **🔌 OutError 补丁**：DLL 437,248B（8/23 01:52）含 OutError 补丁（重名 add 返回非空错误文本、严格拒绝，见 ue5_bridge.py `_cmd_add_component` 注释）；部署于 `Binaries\Win64`，旧版备份 `.bak5`（备份链 .bak~.bak5 完整）
- **🧪 CPP-APIS 在线回归固化（T-20260908 阶段③）**：新增 `tests/test_cpp_apis_online.py` 2 个 `ue_online` 用例（① read_variables 真实清单 Health/MaxHealth 假阳性消除 ② add_component + properties BoxExtent=[200,100,50] → SetComponentProperty → spawn 实测精确生效；token 直读 fixture 免疫 env 污染，模式同 test_blueprint_editor_e2e.py）——离线自动 skip（无需 mock 即可收集通过）；`run_tests.bat e2e/online --ue-online` 已纳入（e2e 批 38 → 40 用例）。将来 DLL/模板改动后**在开发端**跑 `run_tests.bat e2e --ue-online` 即自动回归这 2 个 C++ API 在线路径（第三方发布包不含 `tests/` 与 `run_tests.bat`，此命令仅开发端可用）
>
### v0.8.1 更新日志（T-20260806-UE5AUTOMATION-FIX-AND-VERIFY · 测试缺陷修复）
- **🧹 清理 + docstring 核对（2026-08-20 T-20260820-SKILL-CLEANUP）**：① 删除 scripts/ 23 个临时探针脚本 + tests/ 60+ 个 `_*_tmp.*` 排查残留（2026-08-05 调试遗留）② `ue5_bridge.py` `delete_node` / `_delete_node_on_game_thread` docstring 从过时「三级降级链（graph.remove_node → remove_graph_node → destroy_node）」核对为实测实现（C++ BlueprintPythonBridge.remove_node 优先 + remove_unused_nodes 降级；三个旧 API 在 UE 5.1 Python 绑定实测不存在）——与 T-20260820-REMOVE-NODE-KB 经验条目一致
- **🚀 从零 build → 运行（PIE）端到端实测闭环（2026-08-20 T-20260820-UE5BRIDGE-VERIFY-RUN）**：此前「从零生成蓝图 → 编译 → PIE 运行」链路**从未在线实测过**（验证盲区，2026-08-20 诚实汇报后获用户授权实测）
  - **实测证据链（UE 在线 · dev-project / 5.1.1）**：① `POST /build` 从零生成 BP_Verify_Run5（Actor + VerifyFlag 变量 + 事件开始运行 + 打印字符串 + 连线 + 编译 Status=3）→ ② `spawn_actor_from_class` 进关卡 → ③ Simulate/PIE 注入（`ue_send.py` UDP 注入 `editor_play_simulate`，bridge 无 PIE 端点）→ ④ 日志 `LogBlueprintUserMessages: [BP_Verify_Run5_C_UAID_...] BP_Verify_Run5 RUNNING!` = **BeginPlay 触发 + PrintString 输出成功** → ⑤ 清理
  - **🐛 实测暴露 3 处模板 bug（`game_thread_build.py.tmpl`）**：① `add_event_node` 传参错误（传 bp 应为 graph）；② `add_function_call_node` 缺参（A' 修复只改了 `_cmd_add_function_node` 未同步到模板）；③ `create_actor_blueprint` 已默认创建 BeginPlay 等事件，模板 existing 检查用英文标题找不到中文标题 → 重复添加 → 编译失败。均已修复 + BP_Verify_Run5 重建验证通过
  - **⚠️ 实测经验**：① Actor 蓝图事件函数名是 `ReceiveBeginPlay`（非 BeginPlay）；② 节点标题是本地化中文（「事件开始运行」「打印字符串」），连线/查找必须用中文标题；③ Simulate（SIE）不触发 BeginPlay，且 PIE/Simulate 期间 UDP remote execution 通道失效——PIE 触发需先清残留坏蓝图（编译失败会弹「蓝图编译错误」模态框阻塞 UDP 与 PIE 启动）；④ 客户端 JSON 解析长输出会截断（Unterminated string），需分步短输出执行
- **🐛 Fix 1**：`fake_unreal.BlueprintPythonBridge` 补 `remove_node` API → `test_delete_node_success` 恢复 passed（真根因：fake 缺 RemoveNode，此前验证报告误判为 FakePin 缺 linked_to）
- **🐛 Fix 2**：marker 审计豁免 `ue_offline` → `test_ue_calling_tests_marked_online` 恢复 passed（3 个离线韧性测试不再误判违规）
- **⏱️ Fix 3**：`run_tests.bat`（开发端）`--timeout` 30→90s → `test_ue_send_cli_no_ue`（需 30-60s）不再被误杀中断整个 session
- **🔤 Fix 4**：`run_tests.bat`（开发端）改写为**纯 ASCII 英文注释版** → cmd 任意代码页稳定解析（不再需转码副本；UTF-8 BOM/`chcp 65001`/`PYTHONIOENCODING` 方案实测均失败——cmd 解析碎片化/中文注释卡死）
- **📄 Fix 5**：版本体系统一为 **v0.8.1**（SKILL.md / workflow.md / DEPLOY.md 三文档对齐）
- **🔧 Phase 2 修复（T-20260806-UE5BRIDGE-CRASH-FIX · 异步崩溃 4 次复发修复）**：ue5_bridge asyncio B+D+E+G（9 端点 async→sync + `_gt_worker` 异常字符串化 + trace_id middleware）——v0.8.1 修复 2 项缺陷后 + 异步崩溃 4 次复发修复
  - 离线基线：116 passed · 5 skipped · 0 failed（2026-08-06 复跑确认；2026-08-20 复跑 116 passed · 5 skipped · 0 failed · 51.16s 一致）
  - ✅ **UE 在线 8 项全 PASSED（2026-08-20 实测）**：analyzer_phase2 3 项（phase2a/b/d）+ blueprint_e2e 4 项（update_node_param / connect_pins / disconnect_pin / delete_node · 22.80s）+ mermaid l2_online 1 项（首次实跑 PASSED 1.24s）——8/06 asyncio 崩溃后 B+D+E+G 修复完成，缺陷1/2、SET-FIX、LEFTOVER-EXEC/TAIL 均闭环，UE 零崩溃
  - 验证文档：`docs/VERIFICATION-PHASE2.md`（含 3 轮复跑清单 + 判定标准 + BP_HelloMirror InString 回归检查）
>
### v0.8 更新日志（T-20260805-UE-FULL-CLOSURE · 真验证闭环）
- **🔍 bridge_loaded 真验证**：`ue_send.py --bridge-diag` 改为真实加载探针（① import BlueprintPythonBridge 模块 ② unreal.BlueprintPythonBridge C++ 属性 ③ get_all_graphs/list_nodes 实测），消除"只看目录存在"的误判
- **✅ Phase 2 端到端 PASSED**：真实 UE（dev-project / UE 5.1.1）上 BP_HelloMirror 走通 ue_send 提取 → analyzer feed → JSON 输出全链路（`tests/test_analyzer_phase2.py`）
- **🧪 测试体系扩展**：新增 `test_blueprint_editor_e2e.py`（4 个真实 UE E2E：update_node_param / connect / disconnect / delete，前后回滚）/ `test_analyzer_degrade.py`（JSON 降级容错 L0-L3）/ `test_ue_send_resilience.py`（断连兜底）/ `test_token_auth.py`（4 端点 wrong/no token → 403 契约）/ `test_marker_consistency.py`（ue_online 标记审计）
- **📦 run_tests.bat**（开发端）：新增 `blueprint_editor` / `e2e` 模式（保留原 6 模式）
- **🛠️ 容错增强**：`analyzer.py` 对 basic_info=null / graphs=null 畸形输入兜底（L3 降级不崩）
- **🧪 测试状态**：✅ 100 passed, 1 skipped（baseline）→ v0.8 新增 15+ 项全绿；v0.8.1 修复 2 项缺陷后 116 passed · 5 skipped · 0 failed（Phase 1 实测 2026-08-06）。Phase 2 UE 在线实测（2026-08-06）：`test_analyzer_phase2.py` 3 项 E2E 解锁 **全 PASSED**（此前 skip）；`test_blueprint_editor_e2e.py` 4 项**未解锁**——UE 运行中 uvicorn asyncio 线程 ACCESS VIOLATION 崩溃（`EXCEPTION_ACCESS_VIOLATION`，python39+_asyncio 调用栈，20:59:55），`test_e2e_update_node_param` 写入后 verify/回滚阶段崩、其余 3 项 setup 前 UE 已退出（1 failed + 3 errors）。根因与 ue5_bridge 线程生命周期相关（同 T-20260805-CRASH-ATEXIT-FIX 家族），**待 UE 稳定性修复后复测** → **Phase 2 已修复（B+D+E+G）**，✅ **2026-08-20 全部复跑通过：UE 在线 8/8 PASSED**（analyzer_phase2 3/3 + blueprint_e2e 4/4 单次套件 22.80s + mermaid l2_online 1/1 首次实跑）——按 `docs/VERIFICATION-PHASE2.md` §三 复跑验证闭环
>
### v0.7 更新日志（T-20260727-UE05-V07 · 增量修改模式）
- **🎯 新模式**：§3.5 增量修改模式——4 个核心 API：`update_node_param` / `connect_pins_by_id` / `disconnect_pin` / `delete_node`
- **📦 新模块**：`blueprint_editor.py`（~487 行）—— HTTP Bridge 上层封装，含 freeze→execute→verify 三步保护 + 事务日志
- **🔌 新端点**：`ue5_bridge.py` 新增 `/disconnect`（断线，GameThread 安全）+ `/delete_node`（删节点，含引用检查）
- **📋 能力矩阵**：v0.7 列新增「蓝图续写」「增量修改」「节点删除」「断线/重连」
- **🛡️ 安全**：内建 pitfalls.md 9 坑防护 + KB 命中「跨图同属校验」
- **🧪 测试状态**：离线 Mock 测试 ✅（17/17 通过 · `tests/test_blueprint_editor_mock.py`）| 端到端测试 ✅ 100 passed, 1 skipped (v0.8)
- **🐛 T-20260727-UE05-V07-FIX**：修复 disconnect_pin 对 catalog_only 降级的误判（success=False + warnings）+ 补齐离线测试

