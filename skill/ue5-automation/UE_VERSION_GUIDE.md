# UE 版本适配与升级指南（UE Version Adaptation & Upgrade Guide）

> 随包版本：**v3.1** ｜ 首次随包：T-20260915-UE5PKG-VERSION-DOC
> 定位：**本发布包在 UE 5.1 上实测交付**。本文回答三件事——
> ① **怎么快速判定**你的 UE 版本能不能直接用；② **不能直接用时要动哪一层**（升级路径 B/C）；
> ③ **拿不到源码 / 编译不过时，还剩什么能用**（出口 + 降级），不让人卡死。
>
> 关联文档：`DEPLOY.md`（部署三步）｜`USER_GUIDE.md`（能力上手卡）｜`SKILL.md`（完整手册）

---

## 0. 30 秒速查表

| 你的 UE 版本 | 判定 | 你该走哪条路 | 预估成本 |
|:---|:---:|:---|:---|
| **5.1.x** | ✅ **开箱即用** | 路径 A：按 `DEPLOY.md` 部署即可，**本文其余章节可跳过** | ~10 分钟 |
| 5.2 / 5.3 / 5.4 / 5.5 | ⚠️ **需重编译 bridge** | 路径 B：取源码 → 编译 → 替换 → 跑验收清单（§6） | 1–3 小时（有 C++ 编译经验） |
| **5.8** | ✅ **开箱即用**（随包预编译桥 `bridge/prebuilt-5.8/`，实测 5.8.2） | 路径 A + **5.8 部署差异三要点（`DEPLOY.md` §七·五，必读）**；如需改 C++ 自编译才走路径 B（MSVC ≥ 14.44，UBT 封禁 14.40–14.43）；另随包 `bridge/ue58-extras/` 可在官方 MCP 下开箱用 8 个纯 Python 工具 | ~15 分钟（自编译才 1–3 小时） |
| 5.6 / 5.7 / 6.x / 定制分支 / 引擎源码版 | ❔ **未知** | 路径 C：先按 B 走一遍，按编译错误分类处理（§5） | 半天起 |
| 4.27 及更早 | ❌ **不支持** | 无出口：UEdGraph / Editor 子系统 API 层差异过大，不在适配范围 | — |

> **一句话**：**宿主侧 Python 层跨版本基本不用改；C++ bridge 层必须按目标引擎重编译。**

---

## 1. 为什么必须做版本适配 —— 三层结构（先看清挡在哪一层）

| 层 | 内容 | 是否绑定 UE 版本 | 依据 |
|:---|:---|:---:|:---|
| **L1 · C++ bridge DLL** | `bridge\BlueprintPythonBridge\Binaries\Win64\UnrealEditor-BlueprintPythonBridge.dll`（随包，901,120 B · MD5 `0434743859C997C5711519FCDD449F97` · 2026-09-18 编译） | 🔴 **强绑定** | 用 UE 5.1 头文件与引擎模块编译；直接调用引擎编辑器内部符号（`FBlueprintEditorUtils` / `FKismetEditorUtilities` / `UEdGraph` / `BlueprintGraph`）。换引擎 = 二进制不兼容。 |
| **L2 · UE 内 Python 脚本** | `scripts\ue5_bridge.py`（在 UE 内置 Python 解释器里跑，暴露 HTTP 接口） | 🟡 中 | 只调 `unreal.*` 公开 API；个别 API 在版本间被**废弃/改名**时需要改这一层。 |
| **L3 · 宿主侧 Python 脚本** | `scripts\` 下其余脚本（`ue5_runner` / `blueprint_reader` / `analyzer` / `log_parser` / `project_analyzer` / `preflight` / `init` / `mermaid_render`） | 🟢 **不绑定** | 纯 Python + HTTP/UDP 通信，与引擎版本无关。**换版本时这一层一行都不用改。** |

**推论**：升级成本 ≈ **重编译一个 ~148 KB 的单个 .cpp 模块**，不是重写工具链。这是本包给出的"升级出口"之所以可行的原因。

---

## 2. 第一步：跑版本自检（30 秒，只读）

```cmd
:: 自动探测引擎（注册表 + 常见安装目录）
python scripts\check_ue_version.py

:: 手动指定引擎根
python scripts\check_ue_version.py --engine "C:\Program Files\Epic Games\UE_5.1"

:: 追加在线探测（需 UE 编辑器已启动且 bridge 已加载，读 127.0.0.1:8889/health + /diag）
python scripts\check_ue_version.py --online
```

脚本是**只读**的（不写文件、不改环境变量），退出码可直接用于脚本编排：

| 退出码 | 含义 |
|:---:|:---|
| `0` | UE **5.1.x** → 开箱即用（路径 A） |
| `2` | UE **5.2–5.5** → 需重编译 bridge（路径 B） |
| `3` | 未知/未检测到引擎 → 按路径 C 处理，或手动 `--engine` 指定 |

**数据来源（权威）**：
- 离线：`<引擎根>\Engine\Build\Build.version`（引擎自带 JSON：`MajorVersion` / `MinorVersion` / `PatchVersion` / `Changelist` / `BranchName`）
- 在线：`/health` 的 `ue_info.engine_version`（引擎内 `unreal.SystemLibrary.get_engine_version()`）+ `/diag` 的 `bridge_available` / `method_count` / `key_api_matrix` / `missing_apis`
- 引擎定位顺序：`--engine` → 环境变量 `UE5_ENGINE_PATH` → 注册表 `HKLM\SOFTWARE\EpicGames\Unreal Engine\<版本>` → 常见目录遍历
- 在线探测需 token：`X-Skill-Token` 取自 环境变量 `UE5_BRIDGE_TOKEN` → `%APPDATA%\ue5_bridge\token`（bridge 首启随机生成落盘；与 `ue5_runner` 同口径。⚠️ bridge 端**没有** `test123` 兜底 —— 两侧都读不到 token 时应报错而不是猜默认值）

> ⚠️ **`deploy.py --engine` 的检测范围 ≠ 兼容范围**：它能"发现" 5.2–5.5 的安装位置，但**不会**让 5.1 的 DLL 变得兼容。检测 = 定位，兼容 = 编译。

---

## 3. 路径 A：UE 5.1.x（零改动）

按 `DEPLOY.md` 三步走即可。校验点：

1. `check_ue_version.py` 输出 `5.1.x` 且退出码 `0`
2. 编辑器 Output Log 搜 `BlueprintPythonBridge` 无 "插件加载失败"
3. `python scripts\ue5_runner.py health` → bridge 通（`status: ok`）

若第 3 步不通，问题在**部署/端口/token**，不在版本 —— 去 `docs\SOP.md §6 故障速查`。

---

## 4. 路径 B：UE 5.2–5.5 迁移（推荐出口）

### B-0 先备份

```
备份 <你的项目>\Plugins\BlueprintPythonBridge\  整目录
（或直接换成 5.2+ 的空项目做首次试编译，不动生产项目）
```

### B-1 取得 bridge 源码（**这是唯一的"出口"前提**）

| 文件 | 是否随包 | 说明 |
|:---|:---:|:---|
| `BlueprintPythonBridge.h` | ✅ **随包**（21,619 B） | **接口契约的权威基线**：声明了全部 UFUNCTION（随本 DLL 版本 = 59 个）。你的 `.cpp` 必须实现这里的全部声明。 |
| `BlueprintPythonBridge.cpp` | ❌ 不随包 | 从作者处获取（参考体积 ≈ 148 KB，2026-09-10 审计基线快照口径） |
| `BlueprintPythonBridge.Build.cs` | ❌ 不随包 | 从作者处获取（参考体积 ≈ 1.06 KB）；模块依赖见 B-3 |
| `BlueprintPythonBridge.uplugin` | ✅ **随包** | 模块名 / `Type: Editor` / `LoadingPhase: Default` / 依赖 `PythonScriptPlugin` 已就位 |

> 📮 **出口 ①**：拿不到源码 → **不要卡在这里**，先看 §8「出口与降级」：只读分析 + 项目评估 + 报错诊断这类**不需要 bridge** 的能力仍可用。
> 📮 **出口 ②**：若项目能接受引擎版本变更 → 直接用 UE 5.1 跑，兼容窗口内零成本。

### B-2 组装插件工程

在目标项目里建标准插件目录（`.h` 放 `Public\`，`.cpp` 放 `Private\`；包内扁平布局的 `.h` 可直接搬入）：

```
<你的项目>\Plugins\BlueprintPythonBridge\
    BlueprintPythonBridge.uplugin          ← 随包
    Source\BlueprintPythonBridge\
        BlueprintPythonBridge.Build.cs     ← 作者提供
        Public\BlueprintPythonBridge.h     ← 随包（权威接口）
        Private\BlueprintPythonBridge.cpp   ← 作者提供
```

### B-3 环境准备

- **Visual Studio 2022**（含「使用 C++ 的桌面开发」工作负载）+ 目标 UE 版本的编辑器
- 目标引擎根需有完整 `Engine\Source\`（Launcher 版也会带；若报缺头文件，说明装的是定制/精简版）

`Build.cs` 必须完整声明以下模块依赖（**缺模块 = `LNK2019 无法解析的外部符号`**）：

| 类别 | 模块 |
|:---|:---|
| `PublicDependencyModuleNames` | `Core` · `CoreUObject` · `Engine` · `UnrealEd` · `BlueprintGraph` · `KismetCompiler` · `Kismet` · `InputCore` |
| `PrivateDependencyModuleNames` | `Slate` · `SlateCore` · `PythonScriptPlugin` |

> `InputCore` 是**链接期**必需：`FKey`（`InputCoreTypes.h`，键盘事件节点用）编译期经 `Engine` 传递可见，但链接期不显式依赖会报 `LNK2019`（`FKey::FKey` / `IsValid` / `GetFName` / `~FKey`）。作者 `.Build.cs` 中已含此条并带注释。
> `__has_feature` 宏：该符号是 Clang 内建，MSVC 不识别 → 作者 `.Build.cs` 里已加 `PrivateDefinitions.Add("__has_feature(x)=0")`。**迁移时务必保留**，否则 MSVC 工具链下会报未定义标识符。

### B-4 编译（含一个已被验证的坑）

```cmd
:: 用目标引擎自带的 UBT 编译（示例路径，按你的安装改）
cd "<引擎根>\Engine\Build\BatchFiles"
Build.bat BlueprintPythonBridge Win64 Development -Project="<你的项目>.uproject"
```

> 🕳 **已知坑（已实测验证）**：**UE 5.1 + VS 2022 14.42**（MSVC 14.42.34433）下 UBT 多 `.cpp` 并行编译会概率性抛 `Win32Exception 998 (ERROR_NOACCESS)` → `ManagedProcess` 异常 → 编译中断（与代码内容无关，纯工具链兼容性）。
> **绕行**：改用串行编译
> ```cmd
> Build.bat BlueprintPythonBridge Win64 Development -Project="<你的项目>.uproject" -NoParallelExecutor -MaxParallelActions=1
> ```
> 副作用：编译变慢（本插件单模块，影响可忽略）。证据来源：项目经验库《UE5_VS2022_14.42_NoParallelExecutor_编译修复》（2026-07-22，[已验证]）。

> ⚠️ **诚实声明**：作者侧源码**未使用任何引擎版本分支**（实测 grep `ENGINE_MAJOR_VERSION` / `ENGINE_MINOR_VERSION` / `UE_VERSION` → **0 命中**）。因此**换版本后能否零改动编译通过是未知的** —— 请以编译输出为准，不要假设"应该没问题"。编译不过时按 §5 分类排查。

### B-5 替换 DLL 并验证

```cmd
:: ① 关闭 UE 编辑器（进程未退出前覆盖 DLL 会因文件占用失败）

:: ② 先备份随包原件（以便随时回退到 UE 5.1 可用态）
copy /Y "<你的项目>\Plugins\BlueprintPythonBridge\Binaries\Win64\UnrealEditor-BlueprintPythonBridge.dll" ^
        "<你的项目>\Plugins\BlueprintPythonBridge\Binaries\Win64\UnrealEditor-BlueprintPythonBridge.dll.bak-ue51"

:: ③ 用新编译产物覆盖（UBT 输出目录按你的引擎布局填，通常为
::    <项目>\Binaries\Win64\ 或 <引擎>\Plugins\BlueprintPythonBridge\Binaries\Win64\）
copy /Y "<新编译产物目录>\UnrealEditor-BlueprintPythonBridge.dll" ^
        "<你的项目>\Plugins\BlueprintPythonBridge\Binaries\Win64\UnrealEditor-BlueprintPythonBridge.dll"

:: ④ 启动编辑器 → 跑 §6 六项验收；不通过则用 ② 的 .bak 回退
```

> 📌 **新编译的 DLL 体积与哈希必然与随包不同，这是正常的**。**不要**拿随包 MD5 `0434743859C997C5711519FCDD449F97` 去比对新产物 —— 那个哈希只用于校验"随包这一份是不是 UE 5.1 的原始产物"。

---

## 5. 路径 C：5.6+ / 未来版本 / 定制分支

先按 §4 完整走一遍直到**拿到编译错误**，再按下表分类处理。不要凭猜测改代码。

| 编译/运行症状 | 大概率原因 | 处理 |
|:---|:---|:---|
| `error C2039: 'xxx': 不是 'yyy' 的成员` | 引擎 API 改名 / 移动（`FBlueprintEditorUtils`、`FKismetEditorUtilities` 这类编辑器内部 API 最常变） | 去目标引擎 `Engine\Source\Editor\...` 里 grep 同名符号，按新签名改调用点 |
| `error C2664 / C2660`（参数或重载不匹配） | 同上（签名变更） | 同左，以目标引擎头文件为唯一权威 |
| `LNK2019 无法解析的外部符号` | 模块依赖缺 / 引擎符号改归属 | 在 `Build.cs` 补模块；`InputCore` 是已知易漏项（见 B-3） |
| `error C3861: '__has_feature': 找不到标识符` | MSVC 工具链差异 | 保留 `PrivateDefinitions.Add("__has_feature(x)=0")`（见 B-3） |
| 复制 DLL 后编辑器启动即崩 / 插件加载失败 | ABI 不匹配（DLL 与引擎 Build 号不符） | 确认插件为**编辑器目标**（`.uplugin` 中 `Type: Editor`）、且 DLL 由**同一份**引擎编译 |
| 编辑器里 Python 报 `AttributeError: module 'unreal' has no attribute 'Xxx'` | Python API 废弃/改名 | 见 §7 清单 |
| 蓝图节点创建行为异常（能建但行为不对） | 引擎图节点内部结构变更 | 属引擎级改造，需在 `.cpp` 侧跟进；请在升级记录（§10）中反馈作者 |
| 完全编译不过且短期无解 | — | 走 §8 降级运行，或把项目迁回 5.1 |

---

## 6. 升级后验收清单（6 项，全过才算"可用"）

| # | 检查项 | 命令 / 判据 | 通过标准 |
|:--:|:---|:---|:---|
| 1 | 版本自检 | `python scripts\check_ue_version.py` | 输出 = 你的目标版本 |
| 2 | 插件加载 | 编辑器 Output Log 搜 `BlueprintPythonBridge` | 无 "插件加载失败 / Failed to load" |
| 3 | bridge 接口在位 | `python scripts\check_ue_version.py --online` | `bridge_available = true`，且 `method_count` / `missing_apis` 与 **5.1 基线**对照无异常退化（基线见下方） |
| 4 | 读通道 | `python scripts\ue5_runner.py read /Game/...` 或 `blueprint_reader.py` | 返回节点/变量数据，非空 |
| 5 | **L2 探针真生效** | 看 `blueprint_reader` 输出的 `degrade_level` | **必须为 `L2_BRIDGE`** —— 若停在 `L1_NATIVE` 说明 bridge 实际没生效（工具会自动降级而不报错，这是最易漏的一项） |
| 6 | 错误模式库 | `python scripts\preflight.py` | 配置与 `error_patterns.json` 加载正常 |

> 第 5 项是本清单的关键：读取器有**三级降级链**（`L1_NATIVE` → `L2_BRIDGE` → `L3_MINIMAL`）。L1 只用引擎原生 `unreal` API，**不需要 bridge**，所以"能读出东西"并不能证明 bridge 编译成功 —— 必须看 `degrade_level`。

### 6.1 第 3 项的判据基线（UE 5.1 实测 · 2026-09-15）

| 指标 | 5.1 基线值 | 口径说明 |
|:---|:---:|:---|
| `bridge_available` | `true` | 插件是否加载成功（**首要判据**） |
| `method_count` / `all_methods` | **80** | `dir(bridge)` 去下划线后的属性数 —— **不是** UFUNCTION 数（随包 `.h` 声明的是 59 个 UFUNCTION）；两者口径不同，勿混用 |
| `missing_apis` | **3** | 桥自报的关键 API 缺失数 |
| `key_api_matrix` 中为 `false` 的项 | `add_function_node` · `load_blueprint_asset` · `get_version` | 见 `/diag` 的 `key_api_matrix`；`check_ue_version.py --online` 会直接列出缺失项名 |

> 升级到新引擎版本后，上面这几个数字**本来就可能变化**（方法集随版本增减）—— 它们的作用是"和已知良好基线对照"，**不是**"必须等于 59/80 才通过"。真正的硬判据是 §6 第 4、5 项（能读出数据且 `degrade_level = L2_BRIDGE`）。

---

## 7. Python 侧（L2）适配排查清单

### 7.1 本包实际使用的 `unreal.*` API 与版本风险

| API | 用在哪 | 版本风险 | 备注 |
|:---|:---|:---:|:---|
| `unreal.EditorAssetLibrary`（`load_asset` / `list_assets` / `does_asset_exist` / `save_asset` / `delete_asset` / `find_asset_data`） | bridge / reader / builder / runner 多处 | 🟢 低（5.x 稳定） | `list_assets` 是 **GameThread-only**：本包已用 `_run_on_game_thread_sync` 包裹，迁移时勿绕过 |
| `unreal.get_editor_subsystem(<Subsystem>)` | `blueprint_builder` / `ue5_bridge` | 🟢 低（5.0+ 标准做法） | 本包**未**使用 5.0 起已废弃的单例式 `EditorLevelLibrary` API（实测 grep **0 命中**）→ 向 5.2+ 迁移时少一类断点 |
| `unreal.SystemLibrary.get_engine_version()` / `get_game_name()` | `ue5_bridge` / `ue5_runner` / 自检脚本 | 🟢 低 | 版本自检的数据来源之一 |
| `unreal.BlueprintPythonBridge`（本插件） | `blueprint_reader` / `blueprint_builder` / `analyzer` | 🔴 **取决于 bridge 是否重编译成功** | 未加载时 reader 自动降级 L1（不报错），builder 会失败 |

### 7.2 换版本时的自查命令

```cmd
:: 扫 L2/L3 脚本里的"版本敏感词"
findstr /S /I /C:"EditorLevelLibrary" /C:"SubobjectData" /C:"deprecated" /C:"get_editor_subsystem" scripts\*.py
```

> ⚠️ **本包遗留的已知废弃路径（勿启用）**：`k2_gather_subobject_data_for_blueprint` 相关调用在早期版本验证中被判定为**已废弃且会崩**的路径，本包在线验证流程**主动避开**它。若你迁移到的新引擎版本里该 API 仍存在，**同样不要启用**。

### 7.3 修补的落点

新版本 Python API 若消失/改名，**改 `scripts\ue5_bridge.py`（L2），不要改宿主侧 L3 脚本** —— L3 不应出现 `unreal` 依赖，这是本包的分层契约。改完回到 §6 重跑验收。

---

## 8. 出口与降级（拿不到源码 / 编译不过时，还剩什么能用）

**这是本文最重要的一节**：即使 bridge 完全不可用，本包也**不是全废**。

| 场景 | 是否可用 | 说明 |
|:---|:---:|:---|
| **只读分析蓝图**（结构 / 节点拓扑 / 图表） | ✅ **可用（降级）** | reader 的首级探针 `L1_NATIVE` 只用引擎原生 `unreal.EditorAssetLibrary` + 图遍历，**不经过 bridge DLL**。代价：变量清单 / 连线细节精度下降（全量 `variables` / `connections` 要 L2）。 |
| **项目评估**（架构体检 / 复杂度 / 依赖） | ✅ **可用** | `project_analyzer.py` 为纯 Python（宿主侧扫盘，实测 0 处 `import unreal`） |
| **报错诊断**（Output Log 解析） | ✅ **可用** | `log_parser.py` 纯 Python；它**可选**接受一个 bridge 对象做增强，**缺省不需要** |
| **Mermaid 图表渲染** | ✅ **可用** | `mermaid_render.py` 纯 Python |
| **部署前自检** | ✅ **可用** | `preflight.py` 纯 Python（配置 / 错误模式库加载） |
| **创建 / 修改蓝图**（build / connect / 增删节点 / 编译） | ❌ **不可用** | 全部经 HTTP Bridge（`127.0.0.1:8889`）→ 必须有可用 DLL |

**降级时的义务**：走 L1 降级读取时，请在报告里写明「**L1 降级读取 · 非 L2 精度**」，不要把 L1 结果当作 L2 精度使用。本包读取器的输出里带 `degrade_level` 字段，直接引用它即可。

**其他出口**：
- **出口 ①（首选）**：向作者索取 `BlueprintPythonBridge.cpp` + `.Build.cs`，按 §4 自编译。接口契约以随包 `.h` 为准，可直接比对版本。
- **出口 ②**：项目若能接受引擎版本调整 → 用 UE 5.1 跑，零成本。
- **出口 ③**：短期先用 §8 降级矩阵里的只读能力（分析 / 评估 / 诊断），把写操作排到 bridge 编译成功之后。

---

## 9. 你要改 bridge 接口时（出口的出口）

- 随包 `BlueprintPythonBridge.h`（21,619 B）是**接口基线**：你的 `.cpp` 必须实现其中全部 UFUNCTION 声明，否则 Python 侧会抛 `AttributeError`（且报错点在 L2，容易误判成脚本问题）。
- **新增 / 改名 / 删除 UFUNCTION = 破坏性变更** → 必须同步改 L2 `ue5_bridge.py` 及其调用点（reader / builder / analyzer），并重跑 §6。
- DLL 与 `.h` 的"配套关系"是**哈希级**的：随包这一对是同一编译产物（DLL MD5 `0434743859C997C5711519FCDD449F97` ↔ `.h` MD5 `963999740465602F13498A7E1953C1D1`）。只换 DLL 不换 `.h`（或反之）会造成"Python 以为有、实际没有"的错位。

---

## 10. 升级记录模板（反馈给作者时附上，能显著加速定位）

```text
【UE 版本升级记录】
目标引擎：<Build.version 全文贴此>
插件编译：成功 / 失败
  - 失败时贴：Build.bat 输出前 30 行 + 首个 error 行全文
  - 失败符号（若有）：...
编译命令：Build.bat ... （是否用了 -NoParallelExecutor）
自检输出：python scripts\check_ue_version.py （贴输出）
在线自检：--online 的 bridge_available / all_methods 数量
已尝试的改法：...
```

---

## 附 A · 本文事实来源（可复核）

| 事实 | 来源 |
|:---|:---|
| 随包 DLL = UE 5.1 预编译 · 901,120 B · MD5 `E63B033E…` | 随包文件实测 + `publish.py` 期望值常量 |
| 随包 `.h` = 21,619 B · MD5 `963999740465602F13498A7E1953C1D1` | 随包文件实测（与作者工作区公共头双向吻合） |
| `Build.cs` 模块依赖清单 | 作者侧基线快照实读（2026-09-10 审计留存） |
| 源码无引擎版本分支（grep 0 命中） | 同上快照 `.cpp` 实测 |
| reader 三级降级链 `L1_NATIVE → L2_BRIDGE → L3_MINIMAL` | `scripts\blueprint_reader.py` 实现实读 |
| 宿主侧脚本无 `unreal` 依赖 | `scripts\*.py` 实测（`project_analyzer` / `log_parser` / `mermaid_render` / `preflight`） |
| VS2022 14.42 并行编译异常与绕行参数 | 项目经验库《UE5_VS2022_14.42_NoParallelExecutor_编译修复》（2026-07-22，[已验证]） |
| 引擎版本权威文件 `Engine\Build\Build.version` | 引擎安装目录实测（含 `MajorVersion/MinorVersion/PatchVersion/Changelist/BranchName`） |
| §6.1 基线数字（80 / 3 / 三个 false 项） | 运行中 UE 5.1.1 编辑器的 `127.0.0.1:8889/diag` 实测（2026-09-15） |

## 附 B · 免责边界（如实声明）

1. 本文所有"5.2–5.5"判定为**基于架构分层推导的可行路径**，**未在 5.2–5.5 上逐版本实测**；实测过的只有 **UE 5.1.1**。
2. 本包**不随 bridge 源码**（`.cpp` / `.Build.cs`），因此"自编译"需要先取得源码 —— 这是既有交付边界，见 `DEPLOY.md`。
3. 目标机 UE 内置 Python 仍需**手动**安装一次 `fastapi` / `uvicorn`（装到 UE 自带的 Python，宿主侧 `pip` 装了无效），做法见 `DEPLOY.md` §8 与 `install_dependencies.bat` 步骤 [4/4]。
4. 版本适配后的最终确认，必须在**目标机 + 目标引擎**上跑 §6 六项，文档不能代替实测。
