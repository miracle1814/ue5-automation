# BlueprintPythonBridge 编译指南（5.2–5.8 自助重编译）

> 随包预编译 DLL 为 **UE 5.1**（`Binaries/Win64/UnrealEditor-BlueprintPythonBridge.dll`）。
> UE 5.2–5.8 需要按本文重编译插件 —— 源码完整随包（本目录），一键脚本已备好。

## 一、前置条件

| 项 | 要求 |
|:--|:--|
| UE 引擎 | 目标版本引擎（Epic Launcher 安装版即可，无需引擎源码） |
| 编译器 | **Visual Studio 2022**，含「使用 C++ 的桌面开发」工作负载 |
| 磁盘 | 编译中间产物约 200MB（默认输出到插件目录 `Out/`） |

## 二、一键编译（推荐）

```cmd
cd <发布包>\bridge\BlueprintPythonBridge\Source
build_for_engine.bat "C:\Program Files\Epic Games\UE_5.4"
```

脚本做的事：`RunUAT BuildPlugin` → 产物落 `..\Out\` → **自动复制 DLL 到
`..\Binaries\Win64\`**（旧 DLL 自动备份为 `.bak-<时间戳>`）。

也可先设环境变量再运行：`set UE5_ENGINE_PATH=D:\Epic Games\UE_5.5` → `build_for_engine.bat`

## 三、手动编译（等价）

```cmd
"<UE_ROOT>\Engine\Build\BatchFiles\RunUAT.bat" BuildPlugin ^
    -Plugin="<发布包>\bridge\BlueprintPythonBridge\BlueprintPythonBridge.uplugin" ^
    -Package="<任意输出目录>" ^
    -TargetPlatforms=Win64
```

编译产物：`<输出目录>\BlueprintPythonBridge\Binaries\Win64\UnrealEditor-BlueprintPythonBridge.dll`
→ 覆盖到 `bridge\BlueprintPythonBridge\Binaries\Win64\`（或直接部署进项目的 `Plugins\`）。

## 四、已验证与未验证范围（如实声明）

| 引擎 | 状态 |
|:--|:--|
| 5.1.x | ✅ 官方预编译基线（随包 DLL，实测 5.1.1） |
| 5.2–5.5 | ⚠️ **本源码工程结构上兼容**（依赖全为引擎内置模块），但**未逐版本实测编译**——遇到报错按第五节处理 |
| **5.8** | ✅ **已实测编译并端到端验收**（引擎 5.8.2-56702186 · MSVC 14.44.35207）；预编译产物见 `bridge/prebuilt-5.8/`。若自行重编译：VS2022 产品需 ≥17.14 且 **MSVC ≥14.44**（14.40–14.43 被 UBT 封禁；14.38 撞引擎 C7539）|
| 5.6 / 5.7 | ❔ 未验证（工程结构兼容，按路径 B 试编译） |

## 五、常见编译问题

| 症状 | 处理 |
|:--|:--|
| 找不到 VS / `Could not find toolchain` | 安装 VS2022 桌面 C++ 工作负载；或 `-Compiler=...` 指定 |
| `Win32Exception 998` / 并行编译崩溃 | 把本目录 `BuildConfiguration.xml` 复制到 `%APPDATA%\Unreal Engine\UnrealBuildTool\` 后重试（其中 `bAllowUBTParallelExecutor=false` 即该修复） |
| 高版本引擎 API 改名/签名变动（C2xxx 错误） | 按报错定位 `Private/BlueprintPythonBridge.cpp` 对应函数：常见于 `FKismetEditorUtilities::*`、`FBlueprintEditorUtils::*`、`UK2Node_*` 构造签名。UE 源码（GitHub 同名分支 `Engine/Source/Editor/...`）对照修正即可——不涉及本插件私有 API |
| `PythonScriptPlugin` 模块找不到 | 确认目标引擎启用了 Python Editor Script Plugin（uplugin 已声明依赖，一般自动满足） |
| 编译成功但编辑器不加载 | ① 确认 DLL 与引擎版本严格一致（major.minor）② 部署路径为 `Plugins\BlueprintPythonBridge\Binaries\Win64\` ③ **`UnrealEditor.modules` 必须与 DLL 同目录**（UE 模块清单含 BuildId；缺失 → 日志报 `Incompatible or missing module` 拒载——5.8 实测踩中；`build_for_engine.bat` 已自动同步）|

## 六、验证编译产物

1. 部署 DLL 后重启编辑器；
2. UE Python 控制台执行：`import unreal; print(len([m for m in dir(unreal.BlueprintPythonBridge) if not m.startswith('_')]))`
   —— 期望输出 **60+**（5.1 基线为 80 个含内部名；59 个 UFUNCTION）；
3. 或运行：`python <发布包>\skill\ue5-automation\scripts\check_ue_version.py --online`
   —— `degrade_level` 或 `bridge_apis` 正常即可。

## 七、目录结构

```
Source/
├── BlueprintPythonBridge/
│   ├── BlueprintPythonBridge.Build.cs          # 模块依赖（全为引擎内置模块）
│   ├── Private/
│   │   ├── BlueprintPythonBridge.cpp           # 主实现（59 UFUNCTION）
│   │   ├── BlueprintPythonBridgeModule.cpp
│   │   └── _has_feature_workaround.h
│   └── Public/
│       ├── BlueprintPythonBridge.h             # API 头（与扁平布局的同名头一致）
│       └── BlueprintPythonBridgeModule.h
├── BuildConfiguration.xml                      # VS 工具链/并行编译设置（可复制到用户 UBT 配置目录）
├── build_for_engine.bat                        # 一键编译脚本（参数化 UE_ROOT）
└── BUILD.md                                    # 本文件
```
