# ue58-extras — 把 ue5-automation 能力接入 UE 5.8 官方 MCP

> 适用：**UE 5.8**（官方 `ModelContextProtocol` 插件随引擎提供）。
> 5.1–5.5 用户不需要本目录 —— 那些版本直接走 `ue5_bridge` 自带的
> `/mcp` 端点（见 `skill/ue5-automation/docs/MCP-ACCESS.md`）。

## 这是什么

一个**纯 Content 插件**（无 C++ 模块），在编辑器启动时向官方 MCP 的
Toolset 注册表注册 `UE5AutomationExtras` 工具集，为官方 MCP 会话补充：

| 工具 | 作用 | 是否需要 C++ 桥 |
|:--|:--|:--:|
| `ue5_status` | 链路状态（桥加载与否 + 方法数） | 否 |
| `ue5_pie_state` | PIE 运行状态（is_playing/世界/暂停/时间） | 否 |
| `ue5_logs` | 读取项目日志尾部（pattern/category 过滤） | 否 |
| `ue5_actor_find` | 枚举关卡 Actor（类/名过滤） | 否 |
| `ue5_actor_set_transform` | 传送 Actor + **回读比对**（PIE 可用） | 否 |
| `ue5_compile` | 编译蓝图 + 状态回读 | 否 |
| `ue5_graph_nodes` | **真实回读**图内节点/引脚（验证 DSL 写图结果） | **是** |
| `ue5_graph_connect` | 节点连线 + **拓扑三重验证**（修 exec 线问题的正解） | **是** |

设计口径与 ue5-automation v3.0 一致：**回读比对、fail-loud、绝不假成功**。

## 前置：开启 5.8 官方 MCP 服务（实测必需）

MCP HTTP 服务**默认不自动启动**。在项目的 `Config/DefaultEditorPerProjectUserSettings.ini` 写入：

```ini
[/Script/ModelContextProtocolEngine.ModelContextProtocolSettings]
ServerUrlPath=/mcp
ServerPortNumber=8000
bAutoStartServer=True
```

重启编辑器后监听 `127.0.0.1:8000`（ZCode 等客户端即按此地址接入）。

## 安装（3 步）

```cmd
:: ① 复制插件到你的 5.8 项目
xcopy /E /I "ue5-automation-extras" "<你的项目>\Plugins\ue5-automation-extras"

:: ② 重启 UE 5.8 编辑器（或已在编辑器内：Output Log 的 Cmd 框执行）
py "import init_unreal"

:: ③ 验证：编辑器日志出现
::    [ue5-automation-extras] Registered toolset ue5_automation_toolset.UE5AutomationExtras.
```

注册后，在 MCP 客户端里 `describe_toolset("ue5_automation_toolset.UE5AutomationExtras")`
即可看到 8 个工具（注意：`call_tool` 的 `tool_name` 只传方法名，如 `ue5_pie_state`）。

## 节点级工具的前置：BlueprintPythonBridge C++ 桥

官方 MCP 的蓝图 DSL 有一个已实证的硬缺陷：**写进图的 exec 线是空的、事件节点
可能不生成**（编译干净但运行不执行），根因是 Python 摸不到 `UEdGraph`。本工具集的
`ue5_graph_nodes` / `ue5_graph_connect` 走 **C++ 桥**（`BlueprintPythonBridge`
的 59 个 UFUNCTION）——这是该缺陷的正解。

桥的部署（二选一）：

```cmd
:: A. 自行编译（推荐；源码随 ue5-automation 发布包）
cd <发布包>\bridge\BlueprintPythonBridge\Source
build_for_engine.bat "C:\Program Files\Epic Games\UE_5.8"
::    → 编译产物自动落到 bridge\BlueprintPythonBridge\Binaries\Win64\
:: 然后把整个 bridge\BlueprintPythonBridge\ 复制到 <你的项目>\Plugins\ 下，重启编辑器

:: B. 预编译产物（若随包提供了 bridge/prebuilt-5.8/，同样复制部署）
```

> ⚠️ 编译 5.8 需要 **MSVC ≥ 14.44**（5.8 UBT 封禁 14.40–14.43；14.38 会撞引擎 C7539）
> 且 VS2022 产品本体 ≥ 17.14。**推荐优先使用随包预编译产物**：将 `bridge\prebuilt-5.8\` 下的
> `UnrealEditor-BlueprintPythonBridge.dll` **与 `UnrealEditor.modules`（必须一起！缺失会被 UE 以**
> `Incompatible or missing module` 拒载）复制到 `<项目>\Plugins\BlueprintPythonBridge\Binaries\Win64\`。
> 未部署桥时，6 个纯 Python 工具照常工作；2 个图工具会返回**可读的缺失提示**。

## 验收状态（实测）

**5.8.2 已端到端验收通过**：官方 MCP（:8000）→ `UE5AutomationExtras` 工具集 → C++ 桥 **88 个方法**
（`ue5_status` 回读 bridge_loaded=true / has_umg=true；`ue5_pie_state` / `ue5_actor_find` / `ue5_logs` 实调通过）。

## 与 ZCodeMCPExtras 共存

两者互不冲突（注册不同 Toolset）。若你已装 ZCodeMCPExtras，`repair_exec_chains.py`
的几何修复仍可用于内置工具的产物；而本工具集的 `ue5_graph_connect` 提供的是
**带拓扑验证的正解路径**，优先推荐。
