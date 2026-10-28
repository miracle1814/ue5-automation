# BlueprintPythonBridge 部署手册

> 版本：v0.10.2（2026-09-15 · T-20260915-UE5PKG-VERSION-DOC 增补「升级路径与出口」指针 + 版本自检命令；承接 v0.10.1 的 UE 版本如实声明）
> 适用范围：所有需部署此 UE5 插件的机器，不绑定特定路径。
> 相关：UE 版本适配与升级 → `UE_VERSION_GUIDE.md`（随包）

---

## 一、前置依赖

| 依赖 | 版本要求 | 说明 |
|:---|:---|:---|
| Python | ≥ 3.10 | 运行部署脚本 |
| Unreal Engine | **5.1**（随包预编译 DLL 仅实测 5.1） | 官方 Launcher 版；**5.2~5.5 需源码自行编译**（见下方如实声明） |
| UE Python Script Plugin | 启用 | 编辑器 → Plugins → 搜索 "Python" → 启用 |
| Node.js | ≥ 16 | 可选，仅 Mermaid 图表渲染需要 |
| mermaid-cli | 最新 | `npm install -g @mermaid-js/mermaid-cli` |

> ⚠️ **UE 版本如实声明（T-20260909-SKILL-EVAL-FIX-B · T-20260915-UE5PKG-VERSION-DOC 增补升级出口）**：随包发布的是 **UE 5.1 预编译 DLL**（`bridge\BlueprintPythonBridge\Binaries\Win64\UnrealEditor-BlueprintPythonBridge.dll`，901,120B · MD5 0434743859C997C5711519FCDD449F97 · 2026-09-18 编译），仅在 UE **5.1.x**（实测 5.1.1）验证通过。**发布包不含 bridge 的 `Source\` 源码目录、也无编译脚本**——bridge 源码维护在作者工作区开发目录，未随发布包分发。因此：
> - UE **5.1.x** 用户：开箱即用，按第二章直接部署。
> - UE **5.2~5.5** 用户：需**联系作者获取 bridge 源码**（`BlueprintPythonBridge.cpp` + `.Build.cs`；接口契约以随包 `BlueprintPythonBridge.h` 为准），按目标 UE 版本自行编译 DLL 后替换。本手册 deploy.py 的引擎路径检测/`--engine` 仅用于安装位置定位，**不改变 DLL 兼容性**。
> - UE **5.6+ / 定制分支**用户：先按 5.2~5.5 路径试编译，按编译错误分类处理。
>
> 📘 **升级路径与出口（必读）**：以上只是"结论"。**怎么做、要动哪一层、编译不过时还剩什么能用** —— 见随包 **`UE_VERSION_GUIDE.md`**（原文：《UE 版本适配与升级指南》），含：
> - **三层适配结构**：只有 L1（C++ bridge DLL）强绑定引擎版本，L3（宿主侧 Python）跨版本不用改；
> - **路径 A / B / C** 逐步操作 + `Build.cs` 模块依赖清单 + 已知 VS2022 并行编译坑的绕行参数；
> - **§8 出口与降级矩阵**：拿不到源码 / 编译不过时，只读分析、项目评估、报错诊断仍可用（写蓝图必须 bridge 生效）；
> - **§6 升级后验收 6 项**（含关键判据：读取器 `degrade_level` 必须为 `L2_BRIDGE`）。
>
> 🔍 **先跑一句自检**（只读，30 秒）：
> ```cmd
> python scripts\check_ue_version.py            :: 自动探测引擎并给出下一步
> python scripts\check_ue_version.py --online   :: 追加探测正在运行的编辑器 + bridge 状态
> ```
> 退出码：`0` = 5.1 可直接用 ｜ `2` = 需重编译 bridge ｜ `3` = 版本未知/未检测到引擎。

---

## 二、部署方式

> ⚠️ **deploy.py 运行位置（T-20260909-SKILL-EVAL-FIX-B 修正）**：`deploy.py` 真身**只**位于发布包 `bridge\BlueprintPythonBridge\deploy.py`（skill 的 `scripts\` 目录下**没有** deploy.py）。以下所有 `python deploy.py` 命令均须**先 `cd` 到该目录**再执行：
> ```cmd
> cd <发布包根目录>\bridge\BlueprintPythonBridge
> ```

### 方式 A：部署到 UE 项目（推荐）

```bash
# （在 bridge\BlueprintPythonBridge\ 目录下执行；下同）
# 自动检测项目（从当前目录向上找 .uproject）
python deploy.py

# 手动指定项目
python deploy.py --target "D:/MyProject"

# 指定编译产物源目录
python deploy.py --source "./build/Plugins/BlueprintPythonBridge"
```

### 方式 B：部署到引擎插件目录

```bash
# （在 bridge\BlueprintPythonBridge\ 目录下执行）
# 自动检测引擎路径
python deploy.py --auto-engine

# 手动指定引擎
python deploy.py --engine "C:/Program Files/Epic Games/UE_5.1"
```

### 方式 C：打包 ZIP

```bash
# （在 bridge\BlueprintPythonBridge\ 目录下执行）
python deploy.py --zip
# 生成 BlueprintPythonBridge_<时间戳>.zip
```

### 方式 D：清理中间文件

```bash
# （在 bridge\BlueprintPythonBridge\ 目录下执行）
python deploy.py --clean
```

---

## 三、引擎路径自动检测逻辑

脚本按以下顺序自动查找 UE 引擎：

1. **环境变量** `UE5_ENGINE_PATH` — 最优先，建议提前设置
2. **Windows 注册表** — 读取 Epic Games Launcher 安装记录（5.1~5.5；⚠️ **检测范围 ≠ 兼容范围**，随包 DLL 仅 5.1 实测，见 §一 如实声明）
3. **常见目录遍历** — `C:\Program Files\Epic Games\UE_5.x` 等
4. **失败** — 提示用 `--engine <path>` 手动指定

### 设置环境变量（推荐）

```powershell
# PowerShell（系统级）
[Environment]::SetEnvironmentVariable("UE5_ENGINE_PATH", "D:\Epic Games\UE_5.1", "User")

# CMD
setx UE5_ENGINE_PATH "D:\Epic Games\UE_5.1"
```

---

## 四、项目路径自动检测逻辑

脚本按以下顺序查找 UE 项目：

1. **环境变量** `UE5_PROJECT_PATH` — 可以是 .uproject 文件路径或项目目录
2. **向上查找** — 从当前目录逐级向上找 `.uproject` 文件（最多 10 层）
3. **失败** — 提示用 `--target <path>` 手动指定

---

## 五、新机器初始化（init.py 薄执行器引导 · 推荐入口）

> 🆕 **T-20260909-SKILL-GEN-C-INIT-CONVERGE**：新机器/新宿主机接入本 skill 时，首选
> `scripts/init.py` 薄执行器引导（v1.1，随包分发）——只做**探测 + 产出建议稿 + 人工确认门**，
> **不自动生效、不静默覆盖**。语义详见 SKILL.md §2.5 与 init.py `--help`。

### 5.1 三步流程

```bash
# ① 探测预览（dry-run 只读，不落盘、零副作用）
python scripts/init.py

# ② 落盘建议稿（--apply 写入 output/role_assignments.yaml，status=proposed 未生效）
python scripts/init.py --apply

# ③ 人工确认门（二选一，将 proposed 升级为 confirmed 才作为生效配置）
python scripts/init.py --apply --assign <agent_id>=<role>      # 显式指派核心角色
#   或手编 output/role_assignments.yaml 后：
python scripts/init.py --reconcile --apply                     # 读回手编角色合并后落盘
```

### 5.2 判定规则（确定性，非黑盒）

| 场景 | 结果 | status |
|:---|:---|:---|
| single_agent（≤1 enabled agent） | 宿主 `all_in_one`（deterministic，无选择空间） | `confirmed`（自动生效） |
| multi_agent 自动语义分配 | 各角色按能力探测指派（auto） | `proposed`（**须人工确认门**） |
| `--assign` / 手编 + `--reconcile --apply` | 角色来源升级 manual | `confirmed` |
| 既有 confirmed + 无人工通道 + role_map 变化 | 拒绝落盘（防静默覆盖） | 保留原稿（exit 3） |

### 5.3 产物

| 产物 | 内容 | 位置 |
|:---|:---|:---|
| `role_assignments.yaml` | 角色分配（orchestrator / ue_executor / screenshot_ocr / report_writer / archivist） | `<skill>/output/`（publish 黑名单，防漂移入包） |
| `init_report.md` | health 自检 + 引擎/项目定位报告 | `<skill>/output/` |

### 5.4 后续人工步骤

| 步骤 | 操作 | 负责人 |
|:---|:---|:---|
| 1 | 确认 UE **5.1** 已安装（预编译 DLL 仅实测 5.1；5.2~5.5 需源码自编译，见 §一），`Python Editor Script Plugin` 已启用 | 你 |
| 2 | 确认 `Editor Preferences → Python → Enable Remote Execution` 已勾选 | 你 |
| 3 | 连通性测试：`python scripts/ue5_runner.py health` | maintainer自动执行 |
| 4 | 部署 Bridge 插件：init 报告 verify=通过时可 `--apply` 自动部署；否则按本手册手动拷贝/编译 | 你/maintainer |
| 5 | 重启 UE5 编辑器，验证插件已加载（Edit → Plugins → 搜索 "BlueprintPythonBridge"） | 你 |

---

## 六、maintainer侧部署流程

> 这里的「maintainer侧」指运行 Python Bridge 服务端脚本的机器。

```bash
# 1. 获取 skills 目录
git clone <repo>  # 或直接拷贝 skills/ue5-automation/

# 2. 安装 Python 依赖
cd skills/ue5-automation
pip install -r requirements.txt   # 如果有的话

# 3. 验证环境
python scripts/preflight.py --help
```

---

## 七、UE 侧部署流程

> 这里的「UE 侧」指运行 Unreal Editor 的机器。

```bash
# 1. 部署预编译产物到目标 UE 项目（deploy.py 位于发布包 bridge\BlueprintPythonBridge\ 下，先 cd 到该目录）
cd <发布包根目录>/bridge/BlueprintPythonBridge
python deploy.py --target "<YourProjectPath>"

# 2. 或将 ZIP 发给 UE 机器后解压
unzip BlueprintPythonBridge_*.zip -d "<YourProjectPath>/Plugins/"

# 3. 确认插件文件结构
# <Project>/Plugins/BlueprintPythonBridge/
#   ├── BlueprintPythonBridge.uplugin
#   ├── Binaries/Win64/UnrealEditor-BlueprintPythonBridge.dll   ← 随包预编译 DLL（901,120B · 2026-09-10）
#   └── Source/BlueprintPythonBridge/Public/BlueprintPythonBridge.h
#   （部署内容以 deploy.py 的 DEPLOY_FILES 为准；发布包 bridge 无 Source 源码，部署的是预编译产物）

# 4. 启动 UE 编辑器 → Edit Plugins → 确认 Bridge 已启用

# 5. 重启编辑器使插件生效

# 6. 开启远程执行：Edit → Project Settings → Plugins → Python → ☑ Enable Remote Execution

# 7. 安装 Python 依赖（Agent 侧需要 requests，仅 Mermaid 渲染使用）
#    用你本机的 Python：
pip install -r requirements.txt

#    如遇下载超时（海外 CDN 慢），加清华镜像：
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 8. 🔴 安装 UE 侧 Python 依赖（否则 HTTP Bridge 起不来）
#    注意：ue5_bridge.py 运行在 **UE 内置 Python** 里，依赖必须装到那个解释器，
#    不是你本机的 Python。缺失时 ue5_bridge 启动会打印「FastAPI/uvicorn 未安装」
#    并直接 raise（见 scripts/ue5_bridge.py 顶部 import 兜底）。
#    方式 A（推荐）：UE 编辑器 → Python 控制台执行
#      import subprocess, sys
#      subprocess.call([sys.executable, '-m', 'pip', 'install', 'fastapi', 'uvicorn'])
#    方式 B：命令行调用 UE 内置 Python
#      "<UE安装目录>\Engine\Binaries\ThirdParty\Python3\Win64\python.exe" -m pip install fastapi uvicorn
#    验证：UE Python 控制台 `import fastapi, uvicorn` 无报错；
#          再执行 `import ue5_bridge; ue5_bridge.start()` 应打印监听 127.0.0.1:8889
```

---

## 七·五、UE 5.8 部署差异（v3.2 双引擎实测沉淀 · 必读）

> 以下三点均在 UE 5.8.2 活体验证（2026-09-19 功能实测，见 v3.2 功能实测报告）。
> 5.1 用户不受影响，可跳过本节。

### ① 远程执行设置键改名 + 生效配置层不同

5.8 把设置属性从 `bEnableRemoteExecution` **改名为 `bRemoteExecution`**，
且 ini 写在 `DefaultEditorPerProjectUserSettings.ini` **不生效**——实际生效层是
**`DefaultEngine.ini`**：

```ini
; <项目>\Config\DefaultEngine.ini
[/Script/PythonScriptPlugin.PythonScriptPluginSettings]
bRemoteExecution=true
RemoteExecutionMulticastGroupEndpoint=239.0.0.1:6766
RemoteExecutionMulticastBindAddress=0.0.0.0
```

写入后**重启编辑器**。验证：编辑器日志出现组播侦听，或宿主机
`python scripts\ue_send.py` 探测（`ue_send.send` 远程执行一句 `print`）。

### ② 桥 Python 依赖必须装入「引擎」site-packages（--target）

5.8 内嵌 Python（3.11）与 5.1 环境独立，fastapi/uvicorn/pydantic 等需单独安装。
⚠️ 直接 `pip install` 可能因目录权限回退到**用户目录**
（`%APPDATA%\Python\Python311\site-packages`）——编辑器内嵌解释器是隔离模式，
**看不见用户目录**（表现为 `import fastapi` 过了但依赖链里的包缺）。
必须显式 `--target` 到引擎 site-packages：

```cmd
"<UE安装目录>\Engine\Binaries\ThirdParty\Python3\Win64\python.exe" ^
  -m pip install --target "<UE安装目录>\Engine\Binaries\ThirdParty\Python3\Win64\Lib\site-packages" ^
  fastapi "uvicorn[standard]" pydantic requests
```

### ③ 预编译桥 DLL 与 `UnrealEditor.modules` 成对放置

```cmd
copy bridge\prebuilt-5.8\UnrealEditor-BlueprintPythonBridge.dll  "<项目>\Plugins\BlueprintPythonBridge\Binaries\Win64\"
copy bridge\prebuilt-5.8\UnrealEditor.modules                    "<项目>\Plugins\BlueprintPythonBridge\Binaries\Win64\"
```

缺 `.modules` 会报 `Incompatible or missing module`（被 5.8 加载器拒绝）。
部署后重启编辑器。

### ④ UE 5.8 的 GameThread 严格检查（宿主侧已内置兼容）

5.8 起**引擎与桥 DLL 拒绝非 GameThread 的 unreal/DLL 访问**（5.1 宽容）。
宿主侧 `ue5_bridge.py` 已内置统一 GT 分流（`_gt_run_handler`，含重入保护），
调用方无需任何改动；但**自行扩展命令时**必须遵循同一纪律——参见
`CONTRIBUTING.md` 核心规则第 2 条。

---

## 八、验证步骤

### 1. Health 检查

在 UE 编辑器 Python 控制台执行：

```python
import unreal
# 检查 Bridge 是否可用
print(unreal.BlueprintPythonBridge)  # 应无报错
```

### 2. 读蓝图测试

```bash
python scripts/blueprint_reader.py --help
python scripts/blueprint_reader.py /Game/Test/TestBlueprint
```

### 3. 编译测试

```bash
python scripts/blueprint_builder.py --help
```

---

## 九、常见问题

| 问题 | 原因 | 解决 |
|:---|:---|:---|
| `[ERROR] 编译产物缺失` | 源目录不对 | `--source` 指定正确的编译输出目录 |
| `无法自动检测 UE 引擎路径` | 非标准安装 | 设 `UE5_ENGINE_PATH` 环境变量或用 `--engine` |
| `无法自动检测 UE 项目路径` | 不在项目目录下 | 设 `UE5_PROJECT_PATH` 或用 `--target` |
| `DLL 被占用` | UE 编辑器未关闭 | 关闭 UE 编辑器后重新部署 |
| `Bridge 未加载` | 插件未启用 | 编辑器 → Plugins → 搜索启用 → 重启 |
| `ue_send.py 未找到` | 路径未配置 | 设 `UE_SEND_PATH` 或把 ue_send.py 放 workspace/scripts/ |
| `未发现 UE 节点` | Remote Execution 未启用 | Edit → Project Settings → Plugins → Python → ☑ Enable Remote Execution |

---

## 十、目录结构参考

```
skills/ue5-automation/
├── config/
│   └── error_patterns.json       # 预检陷阱规则
├── scripts/
│   ├── init.py                   # 新机器初始化薄执行器（v1.1 · 探测+建议稿+人工确认门）
│   ├── preflight.py              # 蓝图生成前预检
│   ├── blueprint_builder.py      # 蓝图构建器
│   ├── blueprint_reader.py       # 蓝图读取
│   ├── analyzer.py               # 分析器
│   ├── log_parser.py             # 日志解析
│   ├── mermaid_render.py         # Mermaid 图表渲染
│   ├── project_analyzer.py       # 项目分析
│   ├── ue5_runner.py             # UE5 统一执行器（ue_send 直调）
│   └── ue5_bridge.py             # UE5 Bridge HTTP 服务端（写入通道主入口，v0.7 新增 /disconnect /delete_node）
├── DEPLOY.md                     # 本文件（新机器初始化见第五章）
└── requirements.txt              # Python 依赖（如有）

# ⚠️ deploy.py 不在 scripts\ 下（T-20260909-SKILL-EVAL-FIX-B 修正）：
# 真身位于发布包 bridge\BlueprintPythonBridge\deploy.py（bridge 唯一真源），见第二章运行位置说明。
```

---

## 十一、环境变量速查

| 变量 | 用途 | 示例 |
|:---|:---|:---|
| `UE5_ENGINE_PATH` | UE 引擎根目录 | `D:\Epic Games\UE_5.1` |
| `UE5_PROJECT_PATH` | UE 项目路径或 .uproject 文件 | `D:\MyProject\MyProject.uproject` |
| `UE5_BRIDGE_PORT` | HTTP Bridge 监听端口（默认 8889） | `8889` |
| `UE5_BRIDGE_TOKEN` | HTTP Bridge 认证 token（两端显式统一时设置；**无 test123 默认值** —— bridge 首启随机生成并落盘 `%APPDATA%\ue5_bridge\token`，调用方默认读该文件；env 优先于 token 文件） | `my-token` |
| `UE_SEND_PATH` | `ue_send.py` 完整路径（找不到时用） | `D:\workspace\ue5-automation\scripts\ue_send.py` |
| `COPAW_WORKSPACE` | 工作区根目录（ue5_bridge.py / ue5_runner.py 定位 skill 资源兜底） | `D:\workspace` |
| `QWENPAW_WORKSPACE` | 工作区根目录（setup.py 探测 agent.json 用） | `D:\workspace` |

> 📐 **工作区路径变量契约（T-20260909-SKILL-EVAL-FIX-B · 文档侧声明）**：skill 的若干 Python 脚本
> （`ue5_bridge.py` / `ue5_runner.py` / 发布包根 `setup.py`）在定位工作区资源
> （`config\error_patterns.json`、`agent.json` 等）时，若未显式设置变量，会**回退到
> `%USERPROFILE%\.copaw\workspaces\default`**（开发机默认布局）。**第三方机器没有该目录**，请按需设置：
>
> | 变量 | 读取方 | 默认值（未设置时） | 第三方机器替代值 |
> |:---|:---|:---|:---|
> | `COPAW_WORKSPACE` | `ue5_bridge.py` / `ue5_runner.py` | `~\.copaw\workspaces\default` | 本机存放 `ue5-automation` skill 的工作区根目录 |
> | `QWENPAW_WORKSPACE` | 发布包根 `setup.py` | `~\.copaw\workspaces\default` | 同上（探测 `agent.json` 用） |
> | `UE_SEND_PATH` | `ue5_runner.py` | 自动在 `workspace/scripts/` 下查找 | `ue_send.py` 的完整路径 |
>
> 设置示例（CMD）：
> ```cmd
> setx COPAW_WORKSPACE "D:\workspace"
> setx UE_SEND_PATH "D:\workspace\ue5-automation\scripts\ue_send.py"
> ```
>
> ⚠️ 代码层 `.copaw` 默认值改造（`setup.py:317` / `ue5_bridge.py:1144` / `ue5_runner.py:41`）**不在本次范围**；
> 上表仅为**契约声明**，帮助第三方机器用环境变量覆盖默认路径。代码行为以各脚本实现为准。
