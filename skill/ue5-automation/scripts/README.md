# ue5-automation / scripts 目录

本目录存放 ue5-automation Skill v0.15 的可执行脚本。

## 文件清单

| 文件 | 说明 | 状态 |
|:---|:---|:---:|
| `ue5_runner.py` | **统一执行器** — 读走 ue_send（UDP），写走 HTTP Bridge（ue5_bridge.py 端口8889） | ✅ v0.6 |
| `ue5_bridge.py` | **HTTP Bridge 服务端** — 写入操作（build/connect）通过 FastAPI 在 UE 内后台运行 | ✅ v0.6 |
| `ue_send.py` | **UDP 多播客户端** — 读取操作（health/read）通过 Remote Execution 直发 | ✅ v0.6 |
| `blueprint_reader.py` | 蓝图逆向识别引擎（三级降级 L1/L2/L3，调用 `unreal.BlueprintPythonBridge.*`） | ✅ |
| `blueprint_builder.py` | 蓝图创建引擎（Embedded 模式，Embedded 模式直接 in-process 调用） | ✅ |
| `analyzer.py` | 项目+蓝图双引擎分析器（架构评估、节点拓扑、引用图） | ✅ |
| `log_parser.py` | UE5 日志解析 + 模式匹配 + 蓝图错误提取（25+ 模式） | ✅ |
| `preflight.py` | 蓝图生成前预检脚本（8 项陷阱检查，准入控制） | ✅ |
| `mermaid_render.py` | Mermaid 流程图渲染（报告图表输出） | ✅ |
| `analyzer_models.py` | analyzer.py 的数据模型（Pydantic/dataclass） | ✅ |
| `project_analyzer.py` | 项目级分析（依赖图、循环依赖检测） | ✅ |
| `init.py` | **初始化执行器（薄执行器 v1.1）** — 三级探测 team agents 能力 → 语义分配角色 → 产出 `role_assignments.yaml` 建议稿 + `init_report.md`（生效须人工确认门） | ✅ v1.1 |

> **混合通道架构**（v0.6 唯一通道）：
> - **读/健康检查** → `ue_send` (UDP 多播 6996) 直发 Python 命令
> - **写/创建/连线** → `ue5_bridge` HTTP 服务 (端口 8889)，Agent 侧 `ue5_runner.py` 自动检测并通过 `ensure_http_bridge()` 远程启动
> - 详见 `../SKILL.md` § 2.2

## ue5_runner.py 使用

`ue5_runner.py` 是 Skill 与 UE5 交互的统一入口。

### 前提

1. UE5 编辑器已打开目标项目
2. `Edit → Plugins → Python Editor Script Plugin` 已启用
3. `Editor Preferences → Python → Enable Remote Execution` 已勾选
4. **写入操作需 `BlueprintPythonBridge` 插件已编译部署**（见 `../bridge/`）

### CLI

```bash
# 健康检查（走 ue_send）
python ue5_runner.py health

# 读取蓝图（走 ue_send → L1/L2/L3 降级）
python ue5_runner.py read /Game/TestFinal/ABCDF --level L2

# 创建蓝图（走 HTTP Bridge → 8889）
python ue5_runner.py build spec.json

# 连线（走 HTTP Bridge → 8889）
python ue5_runner.py connect /Game/Blueprints/BP_Test "EventBeginPlay" "then" "PrintString" "execute"
```

### Python API

```python
from ue5_runner import UE5Runner

runner = UE5Runner()

# 读通道（走 ue_send）
print(runner.health())
analysis = runner.read("/Game/TestFinal/ABCDF", level="L2")

# 写通道（走 HTTP Bridge，自动检测并启动）
spec = {
    "name": "BP_HelloRunner",
    "package_path": "/Game/Blueprints/",
    "parent_class": "Actor",
    "events": [{"event_name": "EventBeginPlay"}],        # D-5：别名 name= 等价可用
    "functions": [{"function_name": "PrintString",       # D-8：别名 name= 等价可用
                   "target_class": "/Script/Engine.KismetSystemLibrary"}],  # 亦可直接给 function_path
    "connections": [{
        "src_node": "EventBeginPlay", "src_pin": "then",
        "dst_node": "PrintString", "dst_pin": "execute"
    }]
}
print(runner.build(spec))
```

## preflight.py 使用

```bash
# 单文件检查
python preflight.py <blueprint.json>

# 批量检查
python preflight.py --batch <manifest.json>

# JSON 输出（CI/CD）
python preflight.py <blueprint.json> --json --exit-code
```

详见 `../SKILL.md` § 2.2.1。

## init.py 使用

`init.py` 是 skill 的**初始化执行器（薄执行器 v1.1）**——新机器/新宿主机首次部署时，
探测本机可用 team agents（三级链：list_agents API → agent.json 扫描 → 宿主兜底），
构建能力记录并**语义分配角色**（orchestrator / ue_executor / ue_executor_backup /
screenshot_ocr / report_writer / archivist / all_in_one），产出角色建议稿供人工确认。

> 🔴 **人工确认门**：自动语义分配结果 = **建议稿（status: proposed），不自动生效**。
> 生效须人工确认：`--assign` 显式指派核心角色，或手编
> `output/role_assignments.yaml` 后 `--reconcile --apply`（→ confirmed）。

### CLI

```bash
# 第 1 步 探测预览（dry-run 只读，默认不落盘）
python init.py

# 第 2 步 落盘建议稿（output/role_assignments.yaml, status=proposed）
python init.py --apply

# 显式指派核心角色 → confirmed（--assign 可多次）
python init.py --apply --assign <agent_id>=orchestrator --assign <agent_id>=ue_executor

# 读回手编 role_assignments.yaml 合并后落盘 → confirmed
python init.py --reconcile --apply

# 高级参数
python init.py --deploy-script <path>   # 显式指定 deploy.py（默认三级定位链）
python init.py --api-base <url>         # 覆盖 agent API 地址（默认 localhost:8088）
```

### 产物

| 产物 | 说明 |
|:---|:---|
| `output/role_assignments.yaml` | 角色分配稿（schema v1.1；proposed 建议稿 / confirmed 生效稿） |
| `output/init_report.md` | 初始化检测与分配报告 |

单 agent 环境（探测到 ≤1 个 enabled agent）确定性指派宿主 `all_in_one`，
`status=confirmed`，无需人工确认即可生效。

### 与 publish.py 的关系

`init.py` 与 `publish.py` 职责互补：

| 脚本 | 时机 | 职责 |
|:---|:---|:---|
| `init.py` | 部署后·运行时 | 探测本机 agents 并产出角色配置建议稿（人工确认后生效） |
| `publish.py` | 发布时·打包 | 以运行目录为唯一真源，构建发布包 `ue5-automation-skill-package/`（`init.py` 随包分发） |

即：`publish.py` 负责把 `init.py` **装到部署包**，`init.py` 负责在新机器上**完成初始化**。

## 历史说明

v0.3 之前的 v1 版本曾尝试用纯 ue_send 通道（不走 HTTP），
但因 GameThread 阻塞、线程隔离等问题在 v0.4 起改用**混合通道**，
v0.6 已成为唯一支持架构。

`ue5_bridge.py` 在 v0.6 中**不是废弃**，而是写入通道的服务端实现，
与 `ue5_runner.py` 配合使用。