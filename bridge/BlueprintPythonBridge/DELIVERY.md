# BlueprintPythonBridge C++ 修复 + 编译交付说明

> 🔄 **说明（2026-09-12 追加）**：本文件下方「改动内容 / 交付产物 / 验证清单」为 **2026-07-22 历史交付记录（T-20260722-SKILL-06）**，其中 DLL 尺寸/时间戳为当时值，非当前随包件。
>
> ## 当前随包状态（2026-09-12 同步 · T-20260912-RELEASE-SYNC）
>
> | 项 | 实测值 |
> |:---|:---|
> | DLL | `Binaries\Win64\UnrealEditor-BlueprintPythonBridge.dll` · **901,120 B** |
> | MD5 | `0434743859C997C5711519FCDD449F97` |
> | 编译时间 | **2026-09-10 17:16:59**（UE 5.1 · VS 2022） |
> | 头文件 | `BlueprintPythonBridge.h`（21,619 B）· 源码实测 **59 个 UFUNCTION**（宿主 UE health 实测 `bridge_apis` 80 项） |
> | `.uplugin` | `VersionName = 1.0.1` · 声明依赖 `PythonScriptPlugin` |
> | `.pdb` | **不随包**（> 50 MB，调试符号） |
> | 部署 | 本目录 `install.bat` / `deploy.py`，或 `docs\SOP.md` §4.1 |
>
> ⚠️ 部署 DLL 后须**重启 UE 编辑器**才会加载新 DLL。

> 任务：T-20260722-SKILL-06  
> Agent：dev  
> 交付日期：2026-07-22 22:53

---

## 改动内容

### 修复：UHT Category 约束（编译阻断）

`FBPNodeInfo`、`FBPPinInfo`、`FBPConnectionInfo` 三个 USTRUCT 的 UPROPERTY 缺少 Category 说明符，导致 UHT 拒绝生成反射代码。

**修改文件：** `Public/BlueprintPythonBridge.h`  
**操作：** 所有 `UPROPERTY(BlueprintReadOnly)` → `UPROPERTY(BlueprintReadOnly, Category = "BlueprintPythonBridge")`

### 新增 API（已在逆向分析报告中设计，现编译通过）

| API | 签名 | 用途 |
|:----|:-----|:-----|
| `GetPinInfos` | `(UEdGraph*, const FBPNodeInfo&) → TArray<FBPPinInfo>` | 通过 FBPNodeInfo 远程查询引脚信息 |
| `GetNodeConnections` | `(UEdGraph*) → TArray<FBPConnectionInfo>` | 读取全图连线拓扑 |
| `GetNodeConnectionsForNode` | `(UEdGraph*, const FBPNodeInfo&) → TArray<FBPConnectionInfo>` | 读取单节点连线 |

### 编译信息

- **UE 版本：** 5.1
- **编译工具链：** VS 2022 14.42.34433
- **编译方式：** `-NoParallelExecutor -MaxParallelActions=1`（绕过 VS 14.42 + UE 5.1 的 Win32Exception 998）
- **DLL 时间戳：** 2026-07-22 22:50
- **DLL 大小：** 345,600 bytes (345KB)　⚠️ *2026-07-22 历史值* —— 当前随包件为 **901,120 B**（见文首「当前随包状态」）
- **编译产物位置：** `<YourProject>/BlueprintPythonBridge/Binaries/Win64/`（取决于你的编译输出目录）

---

## 交付产物

`output/BlueprintPythonBridge/`
├── UnrealEditor-BlueprintPythonBridge.dll  (163 KB · 历史值)
├── UnrealEditor-BlueprintPythonBridge.pdb  (调试符号)
├── BlueprintPythonBridge.uplugin           (插件描述)
└── BlueprintPythonBridge.h                 (API 声明)

---

## 验证清单

- [x] UHT 反射代码生成通过
- [x] 4 个 .cpp 文件编译通过（0 error, 2 deprecation warnings）
- [x] 链接通过
- [x] dumpbin 验证导出符号：GetNodeConnections(#28), GetNodeConnectionsForNode(#29), GetPinInfos(#30), ListNodes(#32), ListPins(#33)
- [x] DLL 拷贝到 output 目录

---

## Python 侧验证脚本（需在 UE 编辑器中运行）

```python
import unreal

# 加载蓝图
bp = unreal.EditorAssetLibrary.load_asset('/Game/TestBP')
graph = unreal.BlueprintPythonBridge.get_event_graph(bp)

# 新增 API ①：ListNodes 返回含 node_guid 的 FBPNodeInfo
nodes = unreal.BlueprintPythonBridge.list_nodes(graph)
for n in nodes:
    print(f'[{n.node_class}] {n.node_title} guid={n.node_guid}')

# 新增 API ②：GetPinInfos 通过 FBPNodeInfo 查询引脚
if len(nodes) > 0:
    pins = unreal.BlueprintPythonBridge.get_pin_infos(graph, nodes[0])
    for p in pins:
        print(f'  {p.pin_name} [{p.pin_type}] dir={p.pin_direction}')

# 新增 API ③：GetNodeConnections 全图连线
conns = unreal.BlueprintPythonBridge.get_node_connections(graph)
for c in conns:
    print(f'  {c.from_node_title}.{c.from_pin_name} -> {c.to_node_title}.{c.to_pin_name}')
```

---

## 已知问题

1. **`_build_temp` 目录 DLL 被 UE 编辑器锁定**，待编辑器关闭后手动覆盖
2. **2 个 deprecated API warning**（`DoesPackageExist` 旧签名），不影响功能
3. **VS 2022 14.42 兼容性**：需 `-NoParallelExecutor` 参数绕过 UBT 的 ManagedProcess Win32Exception 998
